from django.db import migrations

def create_rbac_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    
    # Tạo các groups
    admin_group, _ = Group.objects.get_or_create(name='Network Admins')
    readonly_group, _ = Group.objects.get_or_create(name='Read-Only Operators')
    
    # Admin group sẽ được check bằng code is_superuser hoặc logic code.
    # Read-Only sẽ bị hạn chế write.

class Migration(migrations.Migration):

    dependencies = [
        ('auth', '__first__'),
        ('devices', '0006_create_default_backup_schedule'),
    ]

    operations = [
        migrations.RunPython(create_rbac_groups, migrations.RunPython.noop),
    ]
