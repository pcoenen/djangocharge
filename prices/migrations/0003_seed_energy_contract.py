from datetime import date
from decimal import Decimal

from django.db import migrations


def seed(apps, schema_editor):
    EnergyContract = apps.get_model("prices", "EnergyContract")
    if not EnergyContract.objects.exists():
        EnergyContract.objects.create(
            name="Original dyncharge formula",
            valid_from=date(2000, 1, 1),
            consumption_factor=Decimal("1.02"),
            consumption_offset=Decimal("4"),
            vat_rate=Decimal("6"),
            injection_factor=Decimal("0.98"),
            injection_offset=Decimal("-15"),
            notes="Ported from the PHP app: consumption 1.02 x market + 4 EUR/MWh + 6% VAT, "
            "injection 0.98 x market - 15 EUR/MWh.",
        )


class Migration(migrations.Migration):
    dependencies = [("prices", "0002_energy_contract")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
