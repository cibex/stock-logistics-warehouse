# Copyright 2023 Michael Tietz (MT Software) <mtietz@mt-software.de>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from collections import defaultdict
from copy import copy

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.osv import expression
from odoo.tools import float_compare, split_every

from odoo.addons.stock.models.stock_move import PROCUREMENT_PRIORITIES


class StockLocationOrderpoint(models.Model):
    _name = "stock.location.orderpoint"
    _description = "Stock location orderpoint"
    _order = "priority desc, sequence"

    name = fields.Char(
        "Name",
        copy=False,
        required=True,
        readonly=True,
        default=lambda self: self.env["ir.sequence"].next_by_code(
            "stock.location.orderpoint"
        ),
    )
    company_id = fields.Many2one(
        "res.company",
        "Company",
        required=True,
        default=lambda self: self.env.company,
    )
    location_id = fields.Many2one(
        "stock.location",
        "Location",
        ondelete="cascade",
        required=True,
        check_company=True,
        domain='[("company_id", "=", company_id)]',
    )
    trigger = fields.Selection(
        [("auto", "Auto/realtime"), ("manual", "Manual"), ("cron", "Scheduled")],
        "Trigger",
        default="auto",
        required=True,
        help="Auto/realtime orderpoints are triggered on new moves\n"
        "Manual orderpoints are triggered via the orderpoints' view\n"
        "Scheduled orderpoints are triggered via scheduled actions per location",
    )
    replenish_method = fields.Selection(
        [("fill_up", "Fill up")],
        default="fill_up",
        required=True,
        help="Defines how the qty to replenish gets computed\n"
        "Fill up = The replenishment will be triggered when a move is waiting availability "
        "and forecast quantity is negative at the location (i.e. min=0). "
        "The replenished quantity will bring back the forecast quantity to 0 (i.e. max=0) "
        "but will be limited to what is available at the source location "
        "to plan only reservable replenishment moves",
    )
    sequence = fields.Integer("Sequence", default=10)
    route_id = fields.Many2one(
        "stock.location.route",
        string="Preferred Route",
        domain="[('rule_ids.location_id', 'in', [location_id])]",
    )
    group_id = fields.Many2one(
        "procurement.group",
        "Procurement Group",
        copy=False,
        help="Moves created through this orderpoint "
        "will be put in this procurement group. "
        "If none is given, the moves generated by stock rules "
        "will be grouped into one big picking.",
    )
    location_src_id = fields.Many2one(
        "stock.location", compute="_compute_location_src_id", store=True
    )
    active = fields.Boolean("Active", default=True)
    priority = fields.Selection(
        PROCUREMENT_PRIORITIES,
        "Priority",
        default="0",
    )

    _sql_constraints = [
        (
            "location_route_unique",
            "unique(location_id, route_id, company_id, replenish_method)",
            "The combination of Company, Location, Route and Replenish method must be unique",
        )
    ]

    @api.constrains("location_id", "route_id")
    def _check_location_id_route_id(self):
        for orderpoint in self:
            if (
                not orderpoint.route_id
                or orderpoint.location_id in orderpoint.route_id.rule_ids.location_id
            ):
                continue
            raise ValidationError(
                _(
                    "The selected route {} must contain "
                    "a rule where the Destination Location is {}"
                ).format(
                    orderpoint.route_id.display_name,
                    orderpoint.location_id.display_name,
                )
            )

    @api.depends("location_id", "route_id")
    def _compute_location_src_id(self):
        for orderpoint in self:
            location = False
            if orderpoint.location_id and orderpoint.route_id:
                location = orderpoint.location_id._get_source_location_from_route(
                    orderpoint.route_id,
                    "make_to_stock",
                )
            orderpoint.location_src_id = location

    def _prepare_procurement(self, product, qty, date_planned, proc_vals):
        self.ensure_one()
        proc_vals = copy(proc_vals)
        proc_vals.update(
            {
                "date_planned": date_planned or fields.Datetime.now(),
            }
        )
        return self.env["procurement.group"].Procurement(
            product,
            qty,
            product.uom_id,
            self.location_id,
            self.name,
            self.name,
            self.company_id,
            proc_vals,
        )

    def _prepare_procurement_values(self):
        self.ensure_one()
        return {
            "route_ids": self.route_id,
            "date_deadline": False,
            "warehouse_id": self.location_id.get_closest_warehouse(),
            "group_id": self.group_id,
            "priority": self.priority or "0",
            "location_orderpoint_id": self.id,
        }

    def _get_waiting_move_domain(self):
        """
        Returns a domain which selects waiting moves
        which should be replenished by given orderpoints
        """
        domain = [
            ("state", "in", ["confirmed", "partially_available"]),
            ("move_orig_ids", "=", False),
            ("procure_method", "=", "make_to_stock"),
        ]
        location_domains = []
        for orderpoint in self:
            location_domains.append(
                [
                    ("location_id", "child_of", orderpoint.location_id.ids),
                    "!",
                    ("location_dest_id", "child_of", orderpoint.location_id.ids),
                ]
            )
        if location_domains:
            domain = expression.AND([domain, expression.OR(location_domains)])
        return domain

    def _find_potential_moves_to_replenish_by_location(self, products=False):
        """Return a dictionary of products per location that potentially require a replenishment
        based on the fact there are moves not reserved for those products.
        This reduces the list of products for which the quantity will be computed"""
        domain = self._get_waiting_move_domain()
        if products:
            domain = expression.AND([domain, [("product_id", "in", products.ids)]])
        moves_grouped = self.env["stock.move"].read_group(
            domain,
            ["ids:array_agg(id)", "location_id"],
            "location_id",
        )
        return {
            self.env["stock.location"]
            .browse(res["location_id"][0]): self.env["stock.move"]
            .browse(res["ids"])
            for res in moves_grouped
        }

    def _sort_orderpoints(self):
        return self.sorted()

    @api.model
    def _compute_quantities_dict(self, locations, products):
        qties = {}
        for location in locations:
            qties_on_location = qties.setdefault(location, {})
            products = products.with_context(location=location.id)
            for product_id, qties_dict in products._product_available().items():
                product = products.browse(product_id)
                qties_on_location[product] = qties_dict
        return qties

    def _get_qty_to_replenish(
        self, product, qties_on_locations, qty_already_replenished=0
    ):
        """
        Returns a qty to replenish for a given orderpoint and product
        """
        self.ensure_one()
        product.ensure_one()

        if self.replenish_method == "fill_up":
            return self._get_qty_to_replenish_fill_up(
                product, qties_on_locations, qty_already_replenished
            )
        return 0

    def _get_qty_to_replenish_fill_up(
        self, product, qties_on_locations, qty_already_replenished=0
    ):
        if not self.location_src_id:
            return 0

        qties_on_dest = qties_on_locations[self.location_id][product]
        virtual_available_on_dest = qties_on_dest["virtual_available"]
        if (
            float_compare(
                virtual_available_on_dest, 0, precision_rounding=product.uom_id.rounding
            )
            >= 0
        ):
            return 0

        virtual_available_on_dest = abs(virtual_available_on_dest)
        qties_on_src = qties_on_locations[self.location_src_id][product]
        virtual_available_on_src = (
            qties_on_src["virtual_available"] - qties_on_src["incoming_qty"]
        )
        if (
            float_compare(
                virtual_available_on_src,
                0,
                precision_rounding=product.uom_id.rounding,
            )
            <= 0
        ):
            return 0

        qty_to_replenish = virtual_available_on_dest - qty_already_replenished
        return min(qty_to_replenish, virtual_available_on_src)

    def _get_qties_to_replenish(self, moves_by_location):
        products = set()
        for moves in moves_by_location.values():
            products.update(moves.product_id.ids)
        qties_on_locations = self._compute_quantities_dict(
            (self.location_id | self.location_src_id),
            self.env["product.product"].browse(products),
        )
        qties_replenished = defaultdict(lambda: defaultdict(lambda: 0))
        qties_to_replenish = defaultdict(list)
        for orderpoint in self:
            if orderpoint.location_id not in moves_by_location:
                continue

            for product in moves_by_location[orderpoint.location_id].product_id:
                qties_replenished_for_location = qties_replenished[
                    orderpoint.location_id
                ]
                qty_to_replenish = orderpoint._get_qty_to_replenish(
                    product,
                    qties_on_locations,
                    qties_replenished_for_location[product],
                )
                if (
                    float_compare(
                        qty_to_replenish, 0, precision_rounding=product.uom_id.rounding
                    )
                    > 0
                ):
                    qties_to_replenish[orderpoint].append((product, qty_to_replenish))
                    qties_replenished_for_location[product] += qty_to_replenish
        return qties_to_replenish

    def __prepare_procurements(self, moves_by_location):
        qties_to_replenish_by_orderpoint = self._get_qties_to_replenish(
            moves_by_location
        )
        procurements = []
        for orderpoint, qties_to_replenish in qties_to_replenish_by_orderpoint.items():
            proc_vals = orderpoint._prepare_procurement_values()
            for product, qty in qties_to_replenish:
                date_planned = moves_by_location[
                    orderpoint.location_id
                ]._get_location_orderpoint_replenishment_date(product)
                procurements.append(
                    orderpoint._prepare_procurement(
                        product, qty, date_planned, proc_vals
                    )
                )
        return procurements

    def _prepare_procurements(self, products=False):
        moves_by_location = self._find_potential_moves_to_replenish_by_location(
            products
        )
        return self._sort_orderpoints().__prepare_procurements(moves_by_location)

    def run_replenishment(self, products=False):
        """Run the replenishment for all potential products or only a selection"""
        procurements = self._prepare_procurements(products)
        if not procurements:
            return
        self.env["procurement.group"].with_context(from_orderpoint=True).run(
            procurements, raise_user_error=False
        )
        self._after_replenishment()

    def _prepare_to_assign_replenishment_move_domain(self):
        """Returns a domain which selects moves created by a replenishment"""
        domain = [
            ("state", "in", ["confirmed", "partially_available"]),
            ("procure_method", "=", "make_to_stock"),
            ("location_orderpoint_id", "in", self.ids),
        ]
        return domain

    def _assign_replenishment_moves(self):
        """Assigns moves created by the orderpoints"""
        domain = self._prepare_to_assign_replenishment_move_domain()
        moves_to_assign = self.env["stock.move"].search(
            domain, order="priority desc, date asc, id asc"
        )
        for moves_chunk in split_every(100, moves_to_assign.ids):
            self.env["stock.move"].browse(moves_chunk)._action_assign()

    def _after_replenishment(self):
        self._assign_replenishment_moves()

    @api.model
    def _prepare_orderpoint_domain_location(self, location_ids, location_field=False):
        """
        Returns the domain part of the location selection of _get_orderpoints
        :param list int location_ids: list of stock.location ids
        :param str location_field: location_id or location_src_id
            To create the domain searching the specific location field
        """
        ids = location_ids
        if not isinstance(ids, list):
            ids = ids.ids

        location_field = not location_field and "location_id" or location_field
        return [(location_field, "parent_of", ids)]

    def _prepare_orderpoint_domain(
        self, trigger, locations=False, location_field=False
    ):
        """Returns the domain for _get_orderpoints"""
        domain = [("trigger", "=", trigger)]
        if locations:
            domain = expression.AND(
                [
                    domain,
                    self._prepare_orderpoint_domain_location(locations, location_field),
                ]
            )
        return domain

    @api.model
    def _get_orderpoints(self, trigger, locations=False, location_field=False):
        """Returns orderpoints selected by trigger, locations and location_field"""
        domain = self._prepare_orderpoint_domain(trigger, locations, location_field)
        return self.search(domain)

    def _is_location_parent_of(self, location, location_field):
        """
        Checks if one location of the given orderpoints
        is a parent of the given location

        :param location: browse record of stock.location
        :param location_field: should be location_id or location_src_id
            orderpoints location field to check against
        """
        for parent_location in getattr(self, location_field):
            if location.parent_path.startswith(parent_location.parent_path):
                return True

    @api.model
    def run_auto_replenishment(self, products, locations, location_field=False):
        """
        Run the replenishment for all given products
        Selects the right orderpoints by locations and location_field

        :param products: browse record list of product.product
        :param locations: browse record list of stock.location
        :param location_field: should be location_id or location_src_id
        """
        if not locations or not products:
            return
        self = self._get_orderpoints("auto", locations, location_field)
        self.run_replenishment(products)

    @api.model
    def run_cron_replenishment(self, location_ids=False):
        self = self._get_orderpoints("cron", location_ids)
        self.run_replenishment()
