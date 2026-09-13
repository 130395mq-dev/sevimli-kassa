from django.db import migrations, models


class Migration(migrations.Migration):
    """Kechikkan chek belgisi: asl smena yopilgandan keyin kelgan chek."""

    dependencies = [
        ("sales", "0017_kassa_session"),
    ]

    operations = [
        migrations.AddField(
            model_name="sale",
            name="late",
            field=models.BooleanField(
                default=False, help_text="Asl smena yopilgandan keyin keldi"
            ),
        ),
    ]
