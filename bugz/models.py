import datetime
import hashlib
import json
from typing import NamedTuple, Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone


def parse_color(color: str) -> int:
    color = color.strip()
    if color.startswith("#"):
        color = color[1:]
    alpha = 0xFF
    try:
        if len(color) == 3:
            color = int("".join(d + d for d in color), 16)
        elif len(color) == 6:
            color = int(color, 16)
        elif len(color) == 8:
            color = int(color, 16)
            alpha = color & 0xFF
            color >>= 8
        else:
            raise ValueError()
    except ValueError:
        raise ValidationError("Not a valid CSS hex color", code="invalid")
    return (color << 8) | alpha


class ColorField(models.CharField):
    def to_python(self, value):
        color = parse_color(value)
        if color & 0xFF == 0xFF:
            return f"#{color >> 8:06x}"
        return f"#{color:08x}"


class Label(models.Model):
    name = models.CharField(max_length=64)
    description = models.TextField(blank=True)
    color = ColorField(default="#ffffff", max_length=9)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class Ticket(models.Model):
    title = models.CharField(max_length=280)
    description = models.TextField(blank=True)
    authored_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="authored_tickets",
    )
    created_on = models.DateTimeField(default=timezone.now)
    # Is the ticket open or closed.
    open = models.BooleanField(default=True, db_index=True)
    # Is the ticket locked to only accept edits from staff users.
    locked = models.BooleanField(default=False, db_index=True)
    # Who's in charge.
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_tickets",
    )
    # What tickets are blocking this one.
    blocked_by = models.ManyToManyField("self", related_name="blocking", blank=True)
    # This ticket is a duplicate of another ticket.
    dupe_of = models.ForeignKey(
        "self", related_name="dupes", null=True, blank=True, on_delete=models.SET_NULL
    )
    # The ticket labels.
    labels = models.ManyToManyField(Label, blank=True)

    class Meta:
        ordering = ("-created_on",)

    def get_absolute_url(self):
        return reverse("bugz:ticket", args=[self.pk])

    def __str__(self):
        return self.title

    def clean(self):
        if self.pk is not None and self.dupe_of_id == self.pk:
            raise ValidationError({"dupe_of": "A ticket cannot duplicate itself."})
        # TODO: validate that blocked_by does not contain itself.
        # Nontrivial, Django validation of M2M fields is basically nonexistent.


class TicketUpdate(models.Model):
    ticket = models.ForeignKey(Ticket, related_name="updates", on_delete=models.CASCADE)
    authored_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="authored_ticket_updates",
    )
    authored_on = models.DateTimeField(default=timezone.now)
    old_value = models.TextField(blank=True)
    comment = models.TextField(blank=True)

    class Meta:
        ordering = ["authored_on"]


def save_ticket_comment(ticket: Ticket, authored_by, comment: str):
    return TicketUpdate.objects.create(
        ticket=ticket, authored_by=authored_by, comment=comment
    )


def save_ticket_update(ticket: Ticket, authored_by, blocked_by=None, labels=None):
    old_ticket = Ticket.objects.get(pk=ticket.pk)
    now = timezone.now()
    updates = {}

    def compare_and_store(field, getter):
        old_value = getter(getattr(old_ticket, field.name))
        if field.name == "blocked_by":
            if blocked_by is None:
                return
            new_value = blocked_by
        elif field.name == "labels":
            if labels is None:
                return
            new_value = labels
        else:
            new_value = getter(getattr(ticket, field.name))
        if old_value != new_value:
            updates[field.name] = old_value

    for field in Ticket._meta.fields:
        if field.many_to_one:
            compare_and_store(field, lambda e: None if e is None else e.pk)
        else:
            compare_and_store(field, lambda e: e)

    for field in Ticket._meta.many_to_many:
        compare_and_store(field, lambda e: list(e.values_list("pk", flat=True)))

    with transaction.atomic():
        if labels is not None:
            ticket.labels.set(labels)
        if blocked_by is not None:
            ticket.blocked_by.set(blocked_by)
        ticket.save()
        return TicketUpdate.objects.create(
            ticket=ticket,
            authored_by=authored_by,
            authored_on=now,
            old_value=json.dumps(updates),
        )


class Event(NamedTuple):
    pk: str
    authored_by: settings.AUTH_USER_MODEL
    authored_on: datetime.datetime
    field: str
    old_value: Any = None
    new_value: Any = None


def build_ticket_log(ticket: Ticket):
    def build_lookup_qs(qs, pks):
        return {inst.pk: inst for inst in qs.filter(pk__in=pks)}

    updates = (
        TicketUpdate.objects.filter(ticket=ticket)
        .select_related("authored_by")
        .order_by("-authored_on")
    )

    current_state = {
        "title": ticket.title,
        "description": ticket.description,
        "open": ticket.open,
        "locked": ticket.locked,
        "assignee": ticket.assignee_id,
        "dupe_of": ticket.dupe_of_id,
        "labels": set(ticket.labels.values_list("pk", flat=True)),
        "blocked_by": set(ticket.blocked_by.values_list("pk", flat=True)),
    }

    field_updates = [json.loads(u.old_value) if u.old_value else None for u in updates]
    only_updates = [fu for fu in field_updates if fu is not None]
    users = {fu.get("assignee") for fu in only_updates}
    users |= {current_state["assignee"]}
    tickets = {fu.get("dupe_of") for fu in only_updates}
    tickets |= {pk for fu in only_updates for pk in fu.get("blocked_by", [])}
    tickets |= current_state["blocked_by"] | {current_state["dupe_of"]}
    labels = {pk for fu in only_updates for pk in fu.get("labels", [])}
    labels |= current_state["labels"]

    users = build_lookup_qs(get_user_model().objects, users)
    tickets = build_lookup_qs(Ticket.objects.select_related("authored_by"), tickets)
    labels = build_lookup_qs(Label.objects, labels)

    def emit(old, new, many: bool, lookup=None):
        deleted = object()

        def get(el):
            if lookup is None:
                return el
            if el is None:
                return None
            return lookup.get(el, deleted)

        if many:
            added = set(new) - set(old)
            added = {e for el in added if (e := get(el)) is not deleted}
            removed = set(old) - set(new)
            removed = {e for el in removed if (e := get(el)) is not deleted}
            if removed:
                yield removed, None
            if added:
                yield None, added

        else:
            old = get(old)
            new = get(new)
            if old is not deleted and new is not deleted:
                yield old, new

    for update, field_updates in zip(updates, field_updates):
        # Just a comment.
        if field_updates is None:
            yield Event(
                pk=f"comment-{update.pk}",
                authored_by=update.authored_by,
                authored_on=update.authored_on,
                field="comment",
                new_value=update.comment,
            )
            continue

        # Field updates.
        for field, old in field_updates.items():
            new = current_state[field]
            if field == "assignee":
                changes = emit(old, new, many=False, lookup=users)
            elif field == "dupe_of":
                changes = emit(old, new, many=False, lookup=tickets)
            elif field == "labels":
                changes = emit(old, new, many=True, lookup=labels)
            elif field == "blocked_by":
                changes = emit(old, new, many=True, lookup=tickets)
            elif field == "description":
                # Don't emit log for description changes.
                continue
            else:
                changes = emit(old, new, many=False)

            for old_v, new_v in changes:
                h = hashlib.md5(repr((old_v, new_v)).encode()).hexdigest()[:4]
                yield Event(
                    pk=f"event-{update.pk}{h}",
                    authored_by=update.authored_by,
                    authored_on=update.authored_on,
                    field=field,
                    old_value=old_v,
                    new_value=new_v,
                )

            current_state[field] = old
