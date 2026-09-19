from django.db import migrations, models


class Migration(migrations.Migration):
    """Bitta kassa — bitta kompyuter (2026-09-19).

    Kassa tokeni birinchi ulangan kompyuterga biriktiriladi. Boshqa
    kompyuter o'sha token bilan kelsa server uni tanimaydi va kassa
    login-parol so'raydi. Egasi kassani boshqa kompyuterga ko'chirmoqchi
    bo'lsa — o'sha kompyuterda login-parolni kiritadi yoki panelda
    «Kompyuterni bo'shatish» ni bosadi.
    """

    dependencies = [
        ("sales", "0022_receipt_number_per_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="register",
            name="device",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="register",
            name="device_name",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="register",
            name="device_bound_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
