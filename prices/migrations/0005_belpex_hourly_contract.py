from datetime import date
from decimal import Decimal

from django.db import migrations

NAME = "Dynamic contract - Belpex hourly"


def create(apps, schema_editor):
    EnergyContract = apps.get_model("prices", "EnergyContract")
    if not EnergyContract.objects.filter(name=NAME).exists():
        EnergyContract.objects.create(
            name=NAME,
            valid_from=date(2026, 11, 4),
            resolution="hour_mean",
            # Energy component (excl. VAT) = 0.102 x index + 0.408 c/kWh
            #   = (1.02 x index + 4.08 EUR/MWh) / 10
            consumption_factor=Decimal("1.02"),
            consumption_offset=Decimal("4.08"),
            vat_rate=Decimal("6"),
            # Injection terms not in the contract sheet; copied from the previous contract
            injection_factor=Decimal("0.98"),
            injection_offset=Decimal("-15"),
            notes="Energiecomponent (excl. btw): (0,102 * indexatieparameter) + 0,408 c/kWh. "
            "Indexatieparameter: EPEX SPOT Belgium/Belpex (uur).",
        )


def remove(apps, schema_editor):
    apps.get_model("prices", "EnergyContract").objects.filter(name=NAME).delete()


class Migration(migrations.Migration):
    dependencies = [("prices", "0004_contract_resolution")]

    operations = [migrations.RunPython(create, remove)]
