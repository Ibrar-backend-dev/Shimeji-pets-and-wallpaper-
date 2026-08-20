"""
Keeps Category.has_subcategories truthful.

The apps read this flag to decide whether to render a subcategory row, so it is
denormalized rather than computed per request. Maintaining it on write is cheap;
the reconcile command repairs it if anything ever writes around the ORM.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Category, Subcategory


def _refresh_has_subcategories(category_id) -> None:  # noqa: ANN001
    exists = Subcategory.objects.filter(category_id=category_id).exists()
    # update() rather than save(): no signal recursion, one UPDATE, and it does
    # not clobber concurrent edits to other columns. exclude() makes it a no-op
    # when the flag is already correct, which is the common case.
    Category.objects.filter(pk=category_id).exclude(has_subcategories=exists).update(
        has_subcategories=exists
    )


@receiver(post_save, sender=Subcategory, dispatch_uid="catalog_subcat_saved")
def subcategory_saved(sender, instance: Subcategory, **kwargs) -> None:  # noqa: ANN001, ARG001
    transaction.on_commit(lambda: _refresh_has_subcategories(instance.category_id))


@receiver(post_delete, sender=Subcategory, dispatch_uid="catalog_subcat_deleted")
def subcategory_deleted(sender, instance: Subcategory, **kwargs) -> None:  # noqa: ANN001, ARG001
    transaction.on_commit(lambda: _refresh_has_subcategories(instance.category_id))
