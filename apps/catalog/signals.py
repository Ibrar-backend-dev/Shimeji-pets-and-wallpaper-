"""
Keeps Category.has_subcategories truthful.

The apps read this flag to decide whether to render a subcategory row at all, so
it is denormalized rather than recomputed as an EXISTS subquery on every list
request. Maintaining it on write is cheap; reconcile_counts() repairs it if
anything ever writes around the ORM.

The update runs inline, in the same transaction as the subcategory change,
deliberately. Deferring it to transaction.on_commit() would put the UPDATE in a
*separate* transaction: if that second write failed, the flag would be
permanently wrong with nothing to retry it. Inline, the row and its flag commit
or roll back together.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Category, Subcategory


def _refresh_has_subcategories(category_id) -> None:
    exists = Subcategory.objects.filter(category_id=category_id).exists()
    # update() rather than save(): no signal recursion, one statement, and it
    # cannot clobber a concurrent edit to another column. exclude() makes it a
    # no-op when the flag is already right, which is the common case.
    Category.objects.filter(pk=category_id).exclude(has_subcategories=exists).update(
        has_subcategories=exists
    )


@receiver(post_save, sender=Subcategory, dispatch_uid="catalog_subcat_saved")
def subcategory_saved(sender, instance: Subcategory, **kwargs) -> None:
    _refresh_has_subcategories(instance.category_id)


@receiver(post_delete, sender=Subcategory, dispatch_uid="catalog_subcat_deleted")
def subcategory_deleted(sender, instance: Subcategory, **kwargs) -> None:
    _refresh_has_subcategories(instance.category_id)
