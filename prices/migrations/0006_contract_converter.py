import prices.converters
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("prices", "0005_belpex_hourly_contract")]

    operations = [
        migrations.RenameField("energycontract", "resolution", "converter"),
        migrations.AlterField(
            model_name="energycontract",
            name="converter",
            field=models.CharField(
                choices=prices.converters.converter_choices,
                default="quarter",
                help_text="How market prices are grouped and converted for this tariff",
                max_length=50,
            ),
        ),
    ]
