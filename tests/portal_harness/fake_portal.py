"""Fake org.freedesktop.portal.Desktop validating option types like xdg-desktop-portal."""
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
import sys

XML = """
<node>
  <interface name='org.freedesktop.portal.Background'>
    <method name='RequestBackground'>
      <arg type='s' name='parent_window' direction='in'/>
      <arg type='a{sv}' name='options' direction='in'/>
      <arg type='o' name='handle' direction='out'/>
    </method>
  </interface>
</node>
"""

loop = GLib.MainLoop()
report = open(sys.argv[1], "w")

def on_call(conn, sender, path, iface, method, params, invocation):
    opts = params.get_child_value(1)
    cmd = opts.lookup_value("commandline", None)
    typed = opts.lookup_value("commandline", GLib.VariantType("as"))
    report.write(f"wire_type={cmd.get_type_string() if cmd else 'MISSING'}\n")
    report.write(f"typed_as_lookup={'OK' if typed else 'FAIL'}\n")
    report.flush()
    if typed is None:
        invocation.return_dbus_error(
            "org.freedesktop.portal.Error.InvalidArgument",
            f"Expected type 'as' for option 'commandline', got '{cmd.get_type_string()}'")
        loop.quit()
        return
    token = opts.lookup_value("handle_token", GLib.VariantType("s")).get_string()
    req_path = f"/org/freedesktop/portal/desktop/request/{sender[1:].replace('.', '_')}/{token}"
    invocation.return_value(GLib.Variant("(o)", (req_path,)))
    def respond():
        conn.emit_signal(sender, req_path, "org.freedesktop.portal.Request",
                         "Response", GLib.Variant("(ua{sv})", (0, {"autostart": GLib.Variant("b", True)})))
        GLib.timeout_add(300, loop.quit)
        return False
    GLib.timeout_add(50, respond)

def on_bus(conn, name):
    node = Gio.DBusNodeInfo.new_for_xml(XML)
    conn.register_object("/org/freedesktop/portal/desktop", node.interfaces[0], on_call)

Gio.bus_own_name(Gio.BusType.SESSION, "org.freedesktop.portal.Desktop",
                 Gio.BusNameOwnerFlags.NONE, on_bus, None, None)
GLib.timeout_add(20000, loop.quit)
loop.run()
report.close()
