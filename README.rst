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

`Binary packages`_ are available for Debian, Fedora, Red Hat Enterprise
Linux, and Ubuntu.

.. _`Binary packages`: https://olivearchive.org/docs/vmnetx/install/

Installing from source
----------------------

You will need:

* QEMU and KVM
* pygtk2
* Requests
* dateutil
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

1. If building from Git, run ``autoreconf -i``.
2. ``./configure && make && sudo make install``

Executing a virtual machine image
---------------------------------

Click a link to a VMNetX virtual machine.  Your system should launch
VMNetX and start the VM.  When finished, close the virtual machine
window or click the Quit button.

Generating a virtual machine image
----------------------------------

1. Use virt-manager_ to create a QEMU/KVM virtual machine and install
software into it.  When finished, you may either shut down the virtual
machine or suspend it.

2. Use ``vmnetx-generate`` to create a VMNetX virtual machine package
from the libvirt domain XML file.  For example, if you named your
virtual machine "test", you can use::

    vmnetx-generate -n "Test Machine" ~/.config/libvirt/qemu/test.xml \
        package.nxpk

3. Upload the resulting package file to a web server and make note of
the resulting URL.

4. To enable users to execute the virtual machine by clicking a hyperlink,
*without* downloading the entire package, you must create a reference file.
If the URL to your package is ``http://www.example.com/test.nxpk``::

    vmnetx-generate -r http://www.example.com/test.nxpk test.netx

5.  Upload the ``test.netx`` reference file to your web server and publish
a link to it.  Your server should be configured to associate the ``.netx``
extension with the ``application/x-vmnetx-reference+xml`` content type.

.. _virt-manager: http://virt-manager.org/
