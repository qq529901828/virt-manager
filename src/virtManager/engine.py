#
# Copyright (C) 2006 Red Hat, Inc.
# Copyright (C) 2006 Daniel P. Berrange <berrange@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.
#

import gobject
import gtk

import logging
import traceback
import threading
import os

import libvirt
import virtinst
import dbus

from virtManager.about import vmmAbout
from virtManager.halhelper import vmmHalHelper
from virtManager.clone import vmmCloneVM
from virtManager.connect import vmmConnect
from virtManager.connection import vmmConnection
from virtManager.preferences import vmmPreferences
from virtManager.manager import vmmManager
from virtManager.migrate import vmmMigrateDialog
from virtManager.details import vmmDetails
from virtManager.asyncjob import vmmAsyncJob
from virtManager.create import vmmCreate
from virtManager.host import vmmHost
from virtManager.error import vmmErrorDialog
from virtManager.systray import vmmSystray
import virtManager.util as util


# List of packages to look for via packagekit at first startup.
# If this list is empty, no attempt to contact packagekit is made
LIBVIRT_DAEMON = ""
HV_PACKAGE = ""
OTHER_PACKAGES = []
PACKAGEKIT_PACKAGES = []

if LIBVIRT_DAEMON:
    PACKAGEKIT_PACKAGES.append(LIBVIRT_DAEMON)
if HV_PACKAGE:
    PACKAGEKIT_PACKAGES.append(HV_PACKAGE)
if OTHER_PACKAGES:
    PACKAGEKIT_PACKAGES.extend(OTHER_PACKAGES)


def default_uri():
    tryuri = None
    if os.path.exists("/var/lib/xend") and os.path.exists("/proc/xen"):
        tryuri = "xen:///"
    elif (os.path.exists("/dev/kvm") or
          os.path.exists("/usr/bin/qemu") or
          os.path.exists("/usr/bin/qemu-kvm") or
          os.path.exists("/usr/bin/kvm") or
          os.path.exists("/usr/libexec/qemu-kvm")):
        tryuri = "qemu:///system"

    return tryuri

#############################
# PackageKit lookup helpers #
#############################

def check_packagekit(config, errbox):
    """
    Returns None when we determine nothing useful.
    Returns (success, did we just install libvirt) otherwise.
    """
    if not PACKAGEKIT_PACKAGES:
        return

    logging.debug("Asking PackageKit what's installed locally.")
    try:
        session = dbus.SystemBus()

        pk_control = dbus.Interface(
                        session.get_object("org.freedesktop.PackageKit",
                                           "/org/freedesktop/PackageKit"),
                        "org.freedesktop.PackageKit")
    except Exception:
        logging.exception("Couldn't connect to packagekit")
        return

    found = []
    progWin = vmmAsyncJob(config, _do_async_search,
                          [session, pk_control],
                          _("Searching for available hypervisors..."),
                          run_main=False)
    progWin.run()
    error, ignore = progWin.get_error()
    if error:
        return

    found = progWin.get_data()

    not_found = filter(lambda x: x not in found, PACKAGEKIT_PACKAGES)
    logging.debug("Missing packages: %s" % not_found)

    do_install = not_found
    if not do_install:
        if not not_found:
            # Got everything we wanted, try to connect
            logging.debug("All packages found locally.")
            return (True, False)

        else:
            logging.debug("No packages are available for install.")
            return

    msg = (_("The following packages are not installed:\n%s\n\n"
             "These are required to create KVM guests locally.\n"
             "Would you like to install them now?") %
            reduce(lambda x, y: x + "\n" + y, do_install, ""))

    ret = errbox.yes_no(_("Packages required for KVM usage"), msg)

    if not ret:
        logging.debug("Package install declined.")
        return

    try:
        packagekit_install(do_install)
    except Exception, e:
        errbox.show_err(_("Error talking to PackageKit: %s") % str(e),
                        "".join(traceback.format_exc()))
        return

    return (True, LIBVIRT_DAEMON in do_install)

def _do_async_search(session, pk_control, asyncjob):
    found = []
    try:
        for name in PACKAGEKIT_PACKAGES:
            ret_found = packagekit_search(session, pk_control, name)
            found += ret_found

    except Exception, e:
        logging.exception("Error searching for installed packages")
        asyncjob.set_error(str(e), "".join(traceback.format_exc()))

    asyncjob.set_data(found)

def packagekit_install(package_list):
    session = dbus.SessionBus()

    pk_control = dbus.Interface(
                    session.get_object("org.freedesktop.PackageKit",
                                       "/org/freedesktop/PackageKit"),
                        "org.freedesktop.PackageKit.Modify")

    logging.debug("Installing packages: %s" % package_list)
    pk_control.InstallPackageNames(0, package_list, "hide-confirm-search")

def packagekit_search(session, pk_control, package_name):
    tid = pk_control.GetTid()
    pk_trans = dbus.Interface(
                    session.get_object("org.freedesktop.PackageKit", tid),
                    "org.freedesktop.PackageKit.Transaction")

    found = []
    def package(info, package_id, summary):
        found_name = str(package_id.split(";")[0])
        if found_name in PACKAGEKIT_PACKAGES:
            found.append(found_name)

    def error(code, details):
        raise RuntimeError("PackageKit search failure: %s %s" %
                            (code, details))

    def finished(ignore, runtime):
        gtk.main_quit()

    pk_trans.connect_to_signal('Finished', finished)
    pk_trans.connect_to_signal('ErrorCode', error)
    pk_trans.connect_to_signal('Package', package)
    pk_trans.SearchNames("installed", [package_name])

    # Call main() so this function is synchronous
    gtk.main()

    return found



class vmmEngine(gobject.GObject):
    __gsignals__ = {
        "connection-added": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                             [object]),
        "connection-removed": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                               [object])
        }

    def __init__(self, config):
        self.__gobject_init__()

        self.config = config

        self.windowConnect = None
        self.windowPreferences = None
        self.windowAbout = None
        self.windowCreate = None
        self.windowManager = None
        self.windowMigrate = None

        self.connections = {}
        self.err = vmmErrorDialog(None,
                                  0, gtk.MESSAGE_ERROR, gtk.BUTTONS_CLOSE,
                                  _("Unexpected Error"),
                                  _("An unexpected error occurred"))

        self.timer = None
        self.last_timeout = 0

        self.systray = None

        self._tick_thread = None
        self._tick_thread_slow = False
        self._libvirt_support_threading = virtinst.support.support_threading()
        if not self._libvirt_support_threading:
            logging.debug("Libvirt doesn't support threading, skipping.")

        # Counter keeping track of how many manager and details windows
        # are open. When it is decremented to 0, close the app or
        # keep running in system tray if enabled
        self.windows = 0

        self.hal_helper = None
        self.init_systray()

        self.config.on_stats_update_interval_changed(self.reschedule_timer)
        self.config.on_view_system_tray_changed(self.system_tray_changed)

        self.schedule_timer()
        self.load_stored_uris()
        self.tick()


    def init_systray(self):
        if self.systray:
            return

        self.systray = vmmSystray(self.config, self)
        self.systray.connect("action-toggle-manager", self._do_toggle_manager)
        self.systray.connect("action-suspend-domain", self._do_suspend_domain)
        self.systray.connect("action-resume-domain", self._do_resume_domain)
        self.systray.connect("action-run-domain", self._do_run_domain)
        self.systray.connect("action-shutdown-domain", self._do_shutdown_domain)
        self.systray.connect("action-reboot-domain", self._do_reboot_domain)
        self.systray.connect("action-destroy-domain", self._do_destroy_domain)
        self.systray.connect("action-show-console", self._do_show_console)
        self.systray.connect("action-show-details", self._do_show_details)
        self.systray.connect("action-exit-app", self._do_exit_app)

    def system_tray_changed(self, *ignore):
        systray_enabled = self.config.get_view_system_tray()
        if self.windows == 0 and not systray_enabled:
            # Show the manager so that the user can control the application
            self.show_manager()

    def get_hal_helper(self):
        if not self.hal_helper:
            self.hal_helper = vmmHalHelper()
        return self.hal_helper


    # First run helpers

    def add_default_connection(self):
        # Only add default if no connections are currently known
        if self.config.get_connections():
            return

        # Manager fail message
        msg = _("Could not detect a default hypervisor. Make\n"
                "sure the appropriate virtualization packages\n"
                "are installed (kvm, qemu, libvirt, etc.), and\n"
                "that libvirtd is running.\n\n"
                "A hypervisor connection can be manually\n"
                "added via File->Add Connection")

        manager = self.get_manager()
        logging.debug("Determining default libvirt URI")

        ret = None
        did_install_libvirt = False
        try:
            ret = check_packagekit(self.config, self.err)
        except:
            logging.exception("Error talking to PackageKit")

        if ret:
            # We found the default packages via packagekit: use default URI
            ignore, did_install_libvirt = ret
            tryuri = "qemu:///system"

        else:
            tryuri = default_uri()

        if tryuri is None:
            manager.set_startup_error(msg)
            return

        if did_install_libvirt:
            warnmsg = _(
                "Libvirt was just installed, so the 'libvirtd' service will\n"
                "will need to be started. This can be done with one \n"
                "of the following:\n\n"
                "- From GNOME menus: System->Administration->Services\n"
                "- From the terminal: su -c 'service libvirtd restart'\n"
                "- Restart your computer\n\n"
                "virt-manager will connect to libvirt on the next application\n"
                "start up.")
            self.err.ok(_("Libvirt service must be started"), warnmsg)

        self.connect_to_uri(tryuri, autoconnect=True,
                            do_start=not did_install_libvirt)


    def load_stored_uris(self):
        uris = self.config.get_connections()
        if uris != None:
            logging.debug("About to connect to uris %s" % uris)
            for uri in uris:
                self.add_connection(uri)

    def autostart_connections(self):
        for uri in self.connections:
            conn = self.connections[uri]["connection"]
            if conn.get_autoconnect():
                self.connect_to_uri(uri)

    def connect_to_uri(self, uri, readOnly=None, autoconnect=False,
                       do_start=True):
        self.windowConnect = None

        try:
            conn = self._check_connection(uri)
            if not conn:
                # Unknown connection, add it
                conn = self.add_connection(uri, readOnly, autoconnect)

            self.show_manager()
            if do_start:
                conn.open()
            return conn
        except Exception:
            logging.exception("Error connecting to %s" % uri)
            return None

    def _connect_cancelled(self, connect):
        self.windowConnect = None
        if len(self.connections.keys()) == 0:
            self.exit_app()


    def _do_vm_removed(self, connection, hvuri, vmuuid):
        if self.connections[hvuri]["windowDetails"].has_key(vmuuid):
            self.connections[hvuri]["windowDetails"][vmuuid].close()
            del self.connections[hvuri]["windowDetails"][vmuuid]

    def _do_connection_changed(self, connection):
        if connection.get_state() == connection.STATE_ACTIVE or \
           connection.get_state() == connection.STATE_CONNECTING:
            return

        hvuri = connection.get_uri()
        for vmuuid in self.connections[hvuri]["windowDetails"].keys():
            self.connections[hvuri]["windowDetails"][vmuuid].close()
            del self.connections[hvuri]["windowDetails"][vmuuid]
        if self.connections[hvuri]["windowHost"] is not None:
            self.connections[hvuri]["windowHost"].close()
            self.connections[hvuri]["windowHost"] = None
        if (self.windowCreate and self.windowCreate.conn and
            self.windowCreate.conn.get_uri() == hvuri):
            self.windowCreate.close()

    def reschedule_timer(self, ignore1,ignore2,ignore3,ignore4):
        self.schedule_timer()

    def schedule_timer(self):
        interval = self.get_config().get_stats_update_interval() * 1000

        if self.timer != None:
            gobject.source_remove(self.timer)
            self.timer = None

        # No need to use 'safe_timeout_add', the tick should be
        # manually made thread safe
        self.timer = gobject.timeout_add(interval, self.tick)

    def tick(self):
        if not self._libvirt_support_threading:
            return self._tick()

        if self._tick_thread and self._tick_thread.isAlive():
            if not self._tick_thread_slow:
                logging.debug("Tick is slow, not running at requested rate.")
                self._tick_thread_slow = True
            return 1

        self._tick_thread = threading.Thread(name="Tick thread",
                                            target=self._tick, args=())
        self._tick_thread.daemon = True
        self._tick_thread.start()
        return 1

    def _tick(self):
        for uri in self.connections.keys():
            conn = self.connections[uri]["connection"]
            try:
                conn.tick()
            except KeyboardInterrupt:
                raise
            except libvirt.libvirtError, e:
                if (e.get_error_domain() == libvirt.VIR_FROM_REMOTE and
                    e.get_error_code() == libvirt.VIR_ERR_SYSTEM_ERROR):
                    logging.exception("Could not refresh connection %s." % uri)
                    logging.debug("Closing connection since libvirtd "
                                  "appears to have stopped.")
                    util.safe_idle_add(conn.close)
                else:
                    raise
        return 1

    def change_timer_interval(self,ignore1,ignore2,ignore3,ignore4):
        gobject.source_remove(self.timer)
        self.schedule_timer()

    def get_config(self):
        return self.config

    def _do_show_about(self, src):
        self.show_about()
    def _do_show_preferences(self, src):
        self.show_preferences()
    def _do_show_host(self, src, uri):
        self.show_host(uri)
    def _do_show_connect(self, src):
        self.show_connect()
    def _do_connect(self, src, uri):
        self.connect_to_uri(uri)
    def _do_show_details(self, src, uri, uuid):
        self.show_details(uri, uuid)
    def _do_show_create(self, src, uri):
        self.show_create(uri)
    def _do_show_help(self, src, index):
        self.show_help(index)
    def _do_show_console(self, src, uri, uuid):
        self.show_console(uri, uuid)
    def _do_toggle_manager(self, src):
        self.toggle_manager()
    def _do_show_manager(self, src):
        self.show_manager()
    def _do_refresh_console(self, src, uri, uuid):
        self.refresh_console(uri, uuid)
    def _do_save_domain(self, src, uri, uuid):
        self.save_domain(src, uri, uuid)
    def _do_restore_domain(self, src, uri):
        self.restore_domain(src, uri)
    def _do_destroy_domain(self, src, uri, uuid):
        self.destroy_domain(src, uri, uuid)
    def _do_suspend_domain(self, src, uri, uuid):
        self.suspend_domain(src, uri, uuid)
    def _do_resume_domain(self, src, uri, uuid):
        self.resume_domain(src, uri, uuid)
    def _do_run_domain(self, src, uri, uuid):
        self.run_domain(src, uri, uuid)
    def _do_shutdown_domain(self, src, uri, uuid):
        self.shutdown_domain(src, uri, uuid)
    def _do_reboot_domain(self, src, uri, uuid):
        self.reboot_domain(src, uri, uuid)
    def _do_migrate_domain(self, src, uri, uuid):
        self.migrate_domain(uri, uuid)
    def _do_clone_domain(self, src, uri, uuid):
        self.clone_domain(uri, uuid)
    def _do_exit_app(self, src):
        self.exit_app()

    def show_about(self):
        if self.windowAbout == None:
            self.windowAbout = vmmAbout(self.get_config())
        self.windowAbout.show()

    def show_help(self, index):
        try:
            uri = "ghelp:%s" % self.config.get_appname()
            if index:
                uri += "#%s" % index

            logging.debug("Showing help for %s" % uri)
            gtk.show_uri(None, uri, gtk.get_current_event_time())
        except gobject.GError, e:
            logging.error(("Unable to display documentation:\n%s") % e)

    def show_preferences(self):
        if self.windowPreferences == None:
            self.windowPreferences = vmmPreferences(self.get_config())
            self.windowPreferences.connect("action-show-help", self._do_show_help)
        self.windowPreferences.show()

    def show_host(self, uri):
        con = self._lookup_connection(uri)

        if self.connections[uri]["windowHost"] == None:
            manager = vmmHost(self.get_config(), con, self)
            manager.connect("action-show-help", self._do_show_help)
            manager.connect("action-exit-app", self._do_exit_app)
            manager.connect("action-view-manager", self._do_show_manager)
            manager.connect("action-restore-domain", self._do_restore_domain)
            self.connections[uri]["windowHost"] = manager
        self.connections[uri]["windowHost"].show()

    def show_connect(self):
        def connect_wrap(src, *args):
            return self.connect_to_uri(*args)

        if self.windowConnect == None:
            self.windowConnect = vmmConnect(self.get_config(), self)
            self.windowConnect.connect("completed", connect_wrap)
            self.windowConnect.connect("cancelled", self._connect_cancelled)
        self.windowConnect.show()

    def show_console(self, uri, uuid):
        win = self.show_details(uri, uuid)
        if not win:
            return

        win.activate_console_page()

    def refresh_console(self, uri, uuid):
        if not(self.connections[uri]["windowConsole"].has_key(uuid)):
            return

        console = self.connections[uri]["windowConsole"][uuid]
        if not(console.is_visible()):
            return

        console.show()

    def show_details_performance(self, uri, uuid):
        win = self.show_details(uri, uuid)
        if not win:
            return

        win.activate_performance_page()

    def show_details_config(self, uri, uuid):
        win = self.show_details(uri, uuid)
        if not win:
            return

        win.activate_config_page()

    def show_details(self, uri, uuid):
        con = self._lookup_connection(uri)

        if not(self.connections[uri]["windowDetails"].has_key(uuid)):
            try:
                details = vmmDetails(self.get_config(), con.get_vm(uuid), self)
                details.connect("action-save-domain", self._do_save_domain)
                details.connect("action-destroy-domain", self._do_destroy_domain)
                details.connect("action-show-help", self._do_show_help)
                details.connect("action-suspend-domain", self._do_suspend_domain)
                details.connect("action-resume-domain", self._do_resume_domain)
                details.connect("action-run-domain", self._do_run_domain)
                details.connect("action-shutdown-domain", self._do_shutdown_domain)
                details.connect("action-reboot-domain", self._do_reboot_domain)
                details.connect("action-exit-app", self._do_exit_app)
                details.connect("action-view-manager", self._do_show_manager)
                details.connect("action-migrate-domain", self._do_migrate_domain)
                details.connect("action-clone-domain", self._do_clone_domain)

            except Exception, e:
                self.err.show_err(_("Error bringing up domain details: %s") % str(e),
                                  "".join(traceback.format_exc()))
                return None

            self.connections[uri]["windowDetails"][uuid] = details
        self.connections[uri]["windowDetails"][uuid].show()
        return self.connections[uri]["windowDetails"][uuid]

    def get_manager(self):
        if self.windowManager == None:
            self.windowManager = vmmManager(self.get_config(), self)
            self.windowManager.connect("action-suspend-domain", self._do_suspend_domain)
            self.windowManager.connect("action-resume-domain", self._do_resume_domain)
            self.windowManager.connect("action-run-domain", self._do_run_domain)
            self.windowManager.connect("action-shutdown-domain", self._do_shutdown_domain)
            self.windowManager.connect("action-reboot-domain", self._do_reboot_domain)
            self.windowManager.connect("action-destroy-domain", self._do_destroy_domain)
            self.windowManager.connect("action-save-domain", self._do_save_domain)
            self.windowManager.connect("action-migrate-domain", self._do_migrate_domain)
            self.windowManager.connect("action-clone-domain", self._do_clone_domain)
            self.windowManager.connect("action-show-console", self._do_show_console)
            self.windowManager.connect("action-show-details", self._do_show_details)
            self.windowManager.connect("action-show-preferences", self._do_show_preferences)
            self.windowManager.connect("action-show-create", self._do_show_create)
            self.windowManager.connect("action-show-help", self._do_show_help)
            self.windowManager.connect("action-show-about", self._do_show_about)
            self.windowManager.connect("action-show-host", self._do_show_host)
            self.windowManager.connect("action-show-connect", self._do_show_connect)
            self.windowManager.connect("action-connect", self._do_connect)
            self.windowManager.connect("action-refresh-console", self._do_refresh_console)
            self.windowManager.connect("action-exit-app", self._do_exit_app)
        return self.windowManager

    def toggle_manager(self):
        manager = self.get_manager()
        if not manager.close():
            manager.show()

    def show_manager(self):
        self.get_manager().show()

    def increment_window_counter(self):
        self.windows += 1
        logging.debug("window counter incremented to %s" % self.windows)

    def decrement_window_counter(self):
        self.windows -= 1
        logging.debug("window counter decremented to %s" % self.windows)
        # Don't exit if system tray is enabled
        if self.windows <= 0 and not self.systray.is_visible():
            self.exit_app()

    def exit_app(self):
        conns = self.connections.values()
        for conn in conns:
            conn["connection"].close()
        logging.debug("Exiting app normally.")
        gtk.main_quit()

    def wait_for_open(self, uri):
        # Used to ensure connection fully starts before running
        # ONLY CALL FROM WITHIN A THREAD
        conn = self.connect_to_uri(uri)
        conn.connectThreadEvent.wait()
        if conn.state != conn.STATE_ACTIVE:
            return False
        return True

    def show_create(self, uri):
        if self.windowCreate == None:
            create = vmmCreate(self.get_config(), self)
            create.connect("action-show-console", self._do_show_console)
            create.connect("action-show-help", self._do_show_help)
            self.windowCreate = create
        self.windowCreate.show(uri)

    def add_connection(self, uri, readOnly=None, autoconnect=False):
        conn = vmmConnection(self.get_config(), uri, readOnly, self)
        self.connections[uri] = {
            "connection": conn,
            "windowHost": None,
            "windowDetails": {},
            "windowConsole": {},
            "windowClone": None,
            }
        self.connections[uri]["connection"].connect("vm-removed", self._do_vm_removed)
        self.connections[uri]["connection"].connect("state-changed", self._do_connection_changed)
        self.connections[uri]["connection"].tick()
        self.emit("connection-added", conn)
        self.config.add_connection(conn.get_uri())
        if autoconnect:
            conn.set_autoconnect(True)

        return conn

    def remove_connection(self, uri):
        conn = self.connections[uri]["connection"]
        conn.close()
        self.emit("connection-removed", conn)
        del self.connections[uri]
        self.config.remove_connection(conn.get_uri())

    def connect(self, name, callback):
        handle_id = gobject.GObject.connect(self, name, callback)

        if name == "connection-added":
            for uri in self.connections.keys():
                self.emit("connection-added", self.connections[uri]["connection"])

        return handle_id

    def _check_connection(self, uri):
        conn = self.connections.get(uri)
        if conn:
            return conn["connection"]
        return None

    def _lookup_connection(self, uri):
        conn = self._check_connection(uri)
        if not conn:
            raise RuntimeError(_("Unknown connection URI %s") % uri)
        return conn

    def save_domain(self, src, uri, uuid):
        conn = self._lookup_connection(uri)
        vm = conn.get_vm(uuid)
        managed = bool(vm.managedsave_supported)
        do_prompt = self.config.get_confirm_poweroff()

        if managed and conn.is_remote():
            self.err.val_err(_("Saving virtual machines over remote "
                               "connections is not supported with this "
                               "libvirt version or hypervisor."))
            return

        if do_prompt:
            res = self.err.warn_chkbox(
                    text1=_("Are you sure you want to save "
                            "'%s'?" % vm.get_name()),
                    chktext=_("Don't ask me again."),
                    buttons=gtk.BUTTONS_YES_NO)

            response, skip_prompt = res
            if not response:
                return
            self.config.set_confirm_poweroff(not skip_prompt)

        path = None
        if not managed:
            path = util.browse_local(src.window.get_widget("vmm-details"),
                                     _("Save Virtual Machine"),
                                     self.config, conn,
                                     dialog_type=gtk.FILE_CHOOSER_ACTION_SAVE,
                                     browse_reason=self.config.CONFIG_DIR_SAVE)
            if not path:
                return

        progWin = vmmAsyncJob(self.config, self._save_callback,
                              [vm, path],
                              _("Saving Virtual Machine"))
        progWin.run()
        error, details = progWin.get_error()

        if error is not None:
            self.err.show_err(_("Error saving domain: %s") % error, details)

    def _save_callback(self, vm, file_to_save, asyncjob):
        try:
            conn = util.dup_conn(self.config, vm.connection,
                                 return_conn_class=True)
            newvm = conn.get_vm(vm.get_uuid())

            newvm.save(file_to_save)
        except Exception, e:
            asyncjob.set_error(str(e), "".join(traceback.format_exc()))

    def restore_domain(self, src, uri):
        conn = self._lookup_connection(uri)
        if conn.is_remote():
            self.err.val_err(_("Restoring virtual machines over remote "
                               "connections is not yet supported"))
            return

        path = util.browse_local(src.window.get_widget("vmm-manager"),
                                 _("Restore Virtual Machine"),
                                 self.config, conn,
                                 browse_reason=self.config.CONFIG_DIR_RESTORE)

        if not path:
            return

        progWin = vmmAsyncJob(self.config, self.restore_saved_callback,
                              [path, conn], _("Restoring Virtual Machine"))
        progWin.run()
        error, details = progWin.get_error()

        if error is not None:
            self.err.show_err(error, details,
                              title=_("Error restoring domain"))

    def restore_saved_callback(self, file_to_load, conn, asyncjob):
        try:
            newconn = util.dup_conn(self.config, conn,
                                    return_conn_class=True)
            newconn.restore(file_to_load)
        except Exception, e:
            err = (_("Error restoring domain '%s': %s") %
                                  (file_to_load, str(e)))
            details = "".join(traceback.format_exc())
            asyncjob.set_error(err, details)

    def destroy_domain(self, src, uri, uuid):
        conn = self._lookup_connection(uri)
        vm = conn.get_vm(uuid)
        do_prompt = self.config.get_confirm_forcepoweroff()

        if do_prompt:
            res = self.err.warn_chkbox(
                    text1=(_("Are you sure you want to force poweroff '%s'?") %
                           vm.get_name()),
                    text2=_("This will immediately poweroff the VM without "
                            "shutting down the OS and may cause data loss."),
                    chktext=_("Don't ask me again."),
                    buttons=gtk.BUTTONS_YES_NO)

            response, skip_prompt = res
            if not response:
                return
            self.config.set_confirm_forcepoweroff(not skip_prompt)

        logging.debug("Destroying vm '%s'." % vm.get_name())
        try:
            vm.destroy()
        except Exception, e:
            self.err.show_err(_("Error shutting down domain: %s" % str(e)),
                              "".join(traceback.format_exc()))

    def suspend_domain(self, src, uri, uuid):
        conn = self._lookup_connection(uri)
        vm = conn.get_vm(uuid)
        do_prompt = self.config.get_confirm_pause()

        if do_prompt:
            res = self.err.warn_chkbox(
                    text1=_("Are you sure you want to pause "
                            "'%s'?" % vm.get_name()),
                    chktext=_("Don't ask me again."),
                    buttons=gtk.BUTTONS_YES_NO)

            response, skip_prompt = res
            if not response:
                return
            self.config.set_confirm_pause(not skip_prompt)

        logging.debug("Pausing vm '%s'." % vm.get_name())
        try:
            vm.suspend()
        except Exception, e:
            self.err.show_err(_("Error pausing domain: %s" % str(e)),
                              "".join(traceback.format_exc()))

    def resume_domain(self, src, uri, uuid):
        conn = self._lookup_connection(uri)
        vm = conn.get_vm(uuid)

        logging.debug("Unpausing vm '%s'." % vm.get_name())
        try:
            vm.resume()
        except Exception, e:
            self.err.show_err(_("Error unpausing domain: %s" % str(e)),
                              "".join(traceback.format_exc()))

    def run_domain(self, src, uri, uuid):
        conn = self._lookup_connection(uri)
        vm = conn.get_vm(uuid)

        logging.debug("Starting vm '%s'." % vm.get_name())
        try:
            vm.startup()
        except Exception, e:
            self.err.show_err(_("Error starting domain: %s" % str(e)),
                              "".join(traceback.format_exc()))

    def shutdown_domain(self, src, uri, uuid):
        conn = self._lookup_connection(uri)
        vm = conn.get_vm(uuid)
        do_prompt = self.config.get_confirm_poweroff()

        if do_prompt:
            res = self.err.warn_chkbox(
                    text1=_("Are you sure you want to poweroff "
                            "'%s'?" % vm.get_name()),
                    chktext=_("Don't ask me again."),
                    buttons=gtk.BUTTONS_YES_NO)

            response, skip_prompt = res
            if not response:
                return
            self.config.set_confirm_poweroff(not skip_prompt)

        logging.debug("Shutting down vm '%s'." % vm.get_name())
        try:
            vm.shutdown()
        except Exception, e:
            self.err.show_err(_("Error shutting down domain: %s" % str(e)),
                              "".join(traceback.format_exc()))

    def reboot_domain(self, src, uri, uuid):
        conn = self._lookup_connection(uri)
        vm = conn.get_vm(uuid)
        do_prompt = self.config.get_confirm_poweroff()

        if do_prompt:
            res = self.err.warn_chkbox(
                    text1=_("Are you sure you want to reboot "
                            "'%s'?" % vm.get_name()),
                    chktext=_("Don't ask me again."),
                    buttons=gtk.BUTTONS_YES_NO)

            response, skip_prompt = res
            if not response:
                return
            self.config.set_confirm_poweroff(not skip_prompt)

        logging.debug("Rebooting vm '%s'." % vm.get_name())
        no_support = False
        reboot_err = None
        try:
            vm.reboot()
        except Exception, reboot_err:
            no_support = virtinst.support.is_error_nosupport(reboot_err)
            if not no_support:
                self.err.show_err(_("Error rebooting domain: %s" %
                                  str(reboot_err)),
                                  "".join(traceback.format_exc()))

        if not no_support:
            return

        # Reboot isn't supported. Let's try to emulate it
        logging.debug("Hypervisor doesn't support reboot, let's fake it")
        try:
            vm.manual_reboot()
        except:
            logging.exception("Could not fake a reboot")

            # Raise the original error message
            self.err.show_err(_("Error rebooting domain: %s" %
                              str(reboot_err)),
                              "".join(traceback.format_exc()))

    def migrate_domain(self, uri, uuid):
        conn = self._lookup_connection(uri)
        vm = conn.get_vm(uuid)

        if not self.windowMigrate:
            self.windowMigrate = vmmMigrateDialog(self.config, vm, self)

        self.windowMigrate.set_state(vm)
        self.windowMigrate.show()

    def clone_domain(self, uri, uuid):
        con = self._lookup_connection(uri)
        orig_vm = con.get_vm(uuid)
        clone_window = self.connections[uri]["windowClone"]

        try:
            if clone_window == None:
                clone_window = vmmCloneVM(self.get_config(), orig_vm)
                clone_window.connect("action-show-help", self._do_show_help)
                self.connections[uri]["windowClone"] = clone_window
            else:
                clone_window.set_orig_vm(orig_vm)

            clone_window.show()
        except Exception, e:
            self.err.show_err(_("Error setting clone parameters: %s") %
                              str(e), "".join(traceback.format_exc()))


gobject.type_register(vmmEngine)
