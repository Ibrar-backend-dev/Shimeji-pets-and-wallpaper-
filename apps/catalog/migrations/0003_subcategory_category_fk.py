"""
Make "a subcategory belongs to this item's category" a database guarantee.

MediaItem carries both `category_id` and a nullable `subcategory_id`. Nothing in
plain SQL stops those two disagreeing, and the failure is quiet: the item is
listed under a category whose subcategory filter will never return it, so a
screen simply comes up empty with no error anywhere.

`MediaItem.clean()` catches it in the admin and in the ingest service, but an
application-level check is only as good as every future code path remembering to
call it. A composite foreign key onto (id, category_id) — which the
`subcategory_id_category_uniq` constraint in 0001 exists to make possible —
pushes the invariant into Postgres, where a raw INSERT cannot get around it.

Postgres only. SQLite is used for local development and tests, where the model
validation and the test suite cover the same ground; SQLite also cannot add a
constraint to an existing table without rebuilding it.
"""

from django.db import migrations

CONSTRAINT_NAME = "mediaitem_subcat_matches_category"

ADD_SQL = f"""
ALTER TABLE catalog_mediaitem
    ADD CONSTRAINT {CONSTRAINT_NAME}
    FOREIGN KEY (subcategory_id, category_id)
    REFERENCES catalog_subcategory (id, category_id)
    DEFERRABLE INITIALLY DEFERRED;
"""

DROP_SQL = f"ALTER TABLE catalog_mediaitem DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME};"


def add_composite_fk(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(ADD_SQL)


def drop_composite_fk(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DROP_SQL)


class Migration(migrations.Migration):
    dependencies = [("catalog", "0002_seed_features")]

    operations = [
        migrations.RunPython(add_composite_fk, drop_composite_fk),
    ]
