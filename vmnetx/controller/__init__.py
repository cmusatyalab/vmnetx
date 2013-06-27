import gobject

class AbstractController(gobject.GObject):
    __gsignals__ = {

    }
gobject.type_register(AbstractController)
