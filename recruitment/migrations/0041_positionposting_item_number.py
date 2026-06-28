from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0040_alter_examrecord_exam_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="positionposting",
            name="item_number",
            field=models.CharField(blank=True, default="", max_length=100),
            preserve_default=False,
        ),
    ]
