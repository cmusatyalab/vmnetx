VMNetX - Virtual Machine Network Execution
==========================================

VMNetX allows KVM-based virtual machines to be stored on a web server
and executed on-demand on a client system.  The entire disk and memory
state are not downloaded before starting the VM; instead, disk and
memory chunks are retrieved on demand from the web server and then
cached on the client.

VMNetX allows a user to quickly try out some software without needing
to install it.  The software runs on their own computer, so performance
is better than a thin client.  Unlike a traditional virtual machine
image, the user does not need to download gigabytes of data before they
can run the software.

VMNetX is licensed under the GNU General Public License, version 2.

Binary packages
---------------

[Binary packages][binaries] are available for Debian, Fedora, Red Hat
Enterprise Linux, and Ubuntu.

[binaries]: https://olivearchive.org/docs/vmnetx/install/

Installing from source
----------------------

You will need:

* QEMU and KVM
* pygtk2
* Requests
* argparse
* dateutil
* msgpack-python
* PyYAML
* Flask
* spice-gtk Python bindings (optional)
* gtk-vnc Python bindings
* libvirt Python bindings
* dbus-python
* glib2
* libcurl
* libfuse
* libxml2
* lxml
* pkg-config

If building from the Git repository, you will also need:

* Autoconf
* Automake
* libtool

To install:

1. If building from Git, run `autoreconf -i`.
2. `./configure && make && sudo make install`

Executing a virtual machine image
---------------------------------

Click a link to a VMNetX virtual machine.  Your system should launch
VMNetX and start the VM.  When finished, close the virtual machine
window or click the Quit button.

Generating a virtual machine image
----------------------------------

1. Use `vmnetx-generate -a VM-NAME` to create an empty virtual machine
with an appropriate configuration for VMNetX.

2. Use [virt-manager][virt-manager] to install software into the virtual
machine.  The VM is registered under the "QEMU Usermode" libvirt connection,
and can be accessed with `virt-manager -c qemu:///session`.  You may need to
add a virtual floppy or CD-ROM drive to the VM in order to install software.
Depending on the operating system running in the guest, you may also need to
adjust the emulated virtual hardware.  (Note that only [certain
models][hw-models] of virtual hardware will work.)

3. Shut down the virtual machine and delete any virtual CD-ROM or floppy
drives that you have added.  If a suspended VM is desired, restart and then
"Save" the virtual machine.

4. Use `vmnetx-generate` to create a VMNetX virtual machine package
from the libvirt domain XML file.  For example, if you named your
virtual machine "test", you can use:

        vmnetx-generate -n "Test Machine" ~/.config/libvirt/qemu/test.xml package.nxpk

5. Test the virtual machine:

        vmnetx package.nxpk

[virt-manager]: http://virt-manager.org/
[hw-models]: https://github.com/cmusatyalab/vmnetx/wiki/Permitted-virtual-hardware

Publishing a virtual machine image
----------------------------------

1. Upload your `.nxpk` package to a web server and make note of the
resulting URL.

2. To enable users to execute the virtual machine by clicking a hyperlink,
*without* downloading the entire package, you must create a reference file.
If the URL to your package is `http://www.example.com/test.nxpk`:

        vmnetx-generate -r http://www.example.com/test.nxpk test.netx

3.  Upload the `test.netx` reference file to your web server and publish
a link to it.  Your server should be configured to associate the `.netx`
extension with the `application/x-vmnetx-reference+xml` content type.
