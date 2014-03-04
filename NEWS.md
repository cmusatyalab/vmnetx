VMNetX 0.4.3 (2014-03-04)
-------------------------

- Improve performance of memory image loading
- Add example web frontend for `vmnetx-server`

VMNetX 0.4.2 (2013-12-20)
-------------------------

- Add fullscreen button
- Periodically check for new VMNetX release
- Linux: Improve error message in thick-client mode if CPU not supported
- Windows: Draw loading progress bar in taskbar button
- Windows: Install per-user by default
- Windows: Improve platform integration
- Server: Fix authentication failures after 20 VM launches

VMNetX 0.4.1 (2013-11-07)
-------------------------

- Support client on Windows (remote execution only)
- Add administrative interface for terminating a `vmnetx-server` VM

VMNetX 0.4.0 (2013-08-28)
-------------------------

- Add remote execution server
- Add remote execution support to client
- Add application icon
- Allow running multiple instances of the same VM
- Reduce manual editing of domain XML for package generation
- Fix long delays restoring older memory images
- Fix freeze on rejected memory image with qemu >= 1.3
- Fix crashes on Ubuntu 12.04
- Fix crash if host audio unavailable
- Fix mouse rate throttling (broken in 0.3.3)

VMNetX 0.3.3 (2013-06-21)
-------------------------

- Improve display performance
- Add sound support on newer hosts
- Add manual pages
- Fix some minor bugs

VMNetX 0.3.2 (2013-04-26)
-------------------------

- Recover from "black screen" qemu crashes at startup
- Allow limiting the frequency of guest mouse updates
- Add vmnetx-generate option to create a new virt-manager VM

VMNetX 0.3.1 (2013-04-22)
-------------------------

- Compatibility fixes for older host OSes
- Improve signal handling

VMNetX 0.3 (2013-04-10)
-----------------------

- Switch to single-file VM package format
- Bump minimum libvirt to 0.9.8
- Validate domain XML against libvirt 0.9.8 schema
- Protect client from malicious domain XML using restrictive schema
- Send custom HTTP User-Agent
- Remember HTTP cookies for entire session
- Automatically add user to necessary Unix groups for FUSE access
- Fail chunk fetches if ETag or Last-Modified date has changed
- Alert user if I/O errors occur
- Add screenshot button
- Drop unused image segmentation feature
- Many fixes and improvements

VMNetX 0.2 (2012-04-08)
-----------------------

- Initial release
