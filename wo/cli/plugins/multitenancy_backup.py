"""Cement controller for multi-tenancy backups."""

import argparse

from cement.core.controller import CementBaseController, expose

from wo.core.logging import Log


class WOMultitenancyBackupController(CementBaseController):
    """Manage encrypted multi-tenancy fleet backups."""

    class Meta:
        label = 'backup'
        stacked_on = 'multitenancy'
        stacked_type = 'nested'
        description = 'Manage multi-tenancy fleet backups'
        usage = 'wo multitenancy backup <command> [options]'
        arguments = [
            (['site_name'],
                dict(help='domain name for a single-site operation.', nargs='?')),
            (['--db'],
                dict(help='operate on database snapshots only.', action='store_true')),
            (['--files'],
                dict(help='operate on file snapshots only.', action='store_true')),
            (['--all'],
                dict(help='operate on both database and file snapshots.', action='store_true')),
            (['--all-sites'],
                dict(help='restore the complete fleet.', action='store_true')),
            (['--at'],
                dict(help='select the newest snapshot at or before T.', metavar='T')),
            (['--snapshot'],
                dict(help='select a restic snapshot ID.', metavar='ID')),
            (['--operation'],
                dict(help='select a safety operation ID.', metavar='ID')),
            (['--force'],
                dict(help='skip confirmation prompts.', action='store_true')),
            (['--cron'],
                dict(help=argparse.SUPPRESS, action='store_true')),
        ]

    @expose(hide=True)
    def default(self):
        args = getattr(self.app, 'args', None)
        if args is not None and hasattr(args, 'print_help'):
            args.print_help()
        else:
            Log.info(self, self.Meta.usage)

    @expose(hide=True)
    def init(self):
        from wo.cli.plugins.multitenancy_backup_run import cmd_init
        return cmd_init(self)

    @expose(hide=True)
    def run(self):
        from wo.cli.plugins.multitenancy_backup_run import cmd_run
        return cmd_run(self)

    @expose(hide=True)
    def prune(self):
        from wo.cli.plugins.multitenancy_backup_run import cmd_prune
        return cmd_prune(self)

    @expose(hide=True)
    def check(self):
        from wo.cli.plugins.multitenancy_backup_run import cmd_check
        return cmd_check(self)

    @expose(hide=True)
    def list(self):
        from wo.cli.plugins.multitenancy_backup_status import cmd_list
        return cmd_list(self)

    @expose(hide=True)
    def status(self):
        from wo.cli.plugins.multitenancy_backup_status import cmd_status
        return cmd_status(self)

    @expose(hide=True)
    def forget_site(self):
        from wo.cli.plugins.multitenancy_backup_status import cmd_forget_site
        return cmd_forget_site(self)

    @expose(hide=True)
    def restore(self):
        pargs = self.app.pargs
        if getattr(pargs, 'all_sites', False):
            from wo.cli.plugins.multitenancy_backup_fleet import cmd_restore_fleet
            return cmd_restore_fleet(self)
        from wo.cli.plugins.multitenancy_backup_restore import cmd_restore_site
        return cmd_restore_site(self)


__all__ = ['WOMultitenancyBackupController']
