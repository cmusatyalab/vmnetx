import gobject

class AbstractController(gobject.GObject):
    __gsignals__ = {

    }

    def __init__(self):
        gobject.GObject.__init__(self)

        # Publicly readable
        self.have_memory = None

        # Publicly writable
        self.scheme = None
        self.username = None
        self.password = None

    def initialize(self):
        raise NotImplementedError

    def stop_vm(self):
        raise NotImplementedError

    def shutdown(self):
        raise NotImplementedError
gobject.type_register(AbstractController)
