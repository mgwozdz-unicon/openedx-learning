"""Signal handlers for tagging-related model updates."""

from functools import partial

from django.db import transaction
from django.db.models import QuerySet
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver

from openedx_tagging.models.base import ObjectTag, Tag
from openedx_tagging.tasks import (
    emit_content_object_associations_changed_for_object_ids_task,
    emit_content_object_associations_changed_for_tag_task,
)


def _is_explicit_tag_delete(instance: Tag, origin: object, using: str | None) -> bool:
    """
    Return True only for tags explicitly targeted by the delete operation.

    Descendants deleted via CASCADE are skipped here because the explicit root
    tag's handler emits updates for the whole subtree.
    """
    if isinstance(origin, Tag):
        return origin.pk == instance.pk

    if not isinstance(origin, QuerySet) or origin.model is not Tag:
        return False

    explicit_tags = origin.using(using)
    if not explicit_tags.filter(pk=instance.pk).exists():
        return False

    lineage_parts = instance.lineage.rstrip("\t").split("\t")
    ancestor_lineages = ["\t".join(lineage_parts[:index]) + "\t" for index in range(1, len(lineage_parts))]
    if not ancestor_lineages:
        return True

    return not explicit_tags.filter(lineage__in=ancestor_lineages).exists()


@receiver(post_save, sender=Tag)
def tag_post_save(sender, **kwargs):  # pylint: disable=unused-argument
    """
    If a tag is updated, enqueue async event emission for all associated objects.
    """
    instance = kwargs.get("instance", None)

    if kwargs.get("created", False) or instance is None:
        return

    tag_id = instance.id
    if tag_id is None:
        return

    transaction.on_commit(
        partial(
            emit_content_object_associations_changed_for_tag_task.delay,
            tag_id=tag_id
        ),
    )


@receiver(pre_delete, sender=Tag)
def tag_pre_delete(sender, **kwargs):  # pylint: disable=unused-argument
    """
    If a tag is deleted, enqueue async event emission for all associated objects.
    """
    instance = kwargs.get("instance", None)
    origin = kwargs.get("origin", None)
    using = kwargs.get("using", None)

    if instance is None or instance.id is None:
        return

    if not _is_explicit_tag_delete(instance, origin, using):
        return

    object_ids = list(
        ObjectTag.objects.using(using)
        .filter(tag__lineage__startswith=instance.lineage)
        .values_list("object_id", flat=True)
        .distinct()
    )
    if not object_ids:
        return

    transaction.on_commit(
        partial(
            emit_content_object_associations_changed_for_object_ids_task.delay,
            object_ids=object_ids,
        ),
        using=using,
    )
