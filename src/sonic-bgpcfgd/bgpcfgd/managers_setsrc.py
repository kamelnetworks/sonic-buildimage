import jinja2

from .log import log_err, log_info
from .manager import Manager
from .template import TemplateFabric


class ZebraSetSrc(Manager):
    """ This class initialize "set src" settings for zebra """
    def __init__(self, common_objs, db, table):
        """
        Initialize the object
        :param common_objs: common object dictionary
        :param db: name of the db
        :param table: name of the table in the db
        """
        super(ZebraSetSrc, self).__init__(
            common_objs,
            [],
            db,
            table,
        )
        tf = common_objs['tf']
        self.zebra_set_src_template = tf.from_file("zebra/zebra.set_src.conf.j2")
        self.lo_ipv4 = None
        self.lo_ipv6 = None
        self.lo_ipv4_key = None
        self.lo_ipv6_key = None

    def set_handler(self, key, data):
        """ Implementation of 'SET' command for this class """
        self.directory.put(self.db_name, self.table_name, key, data)
        #
        if key.startswith("Loopback0|") and "state" in data and data["state"] == "ok":
            ip_addr_w_mask = key.replace("Loopback0|", "")
            slash_pos = ip_addr_w_mask.rfind("/")
            if slash_pos == -1:
                log_err("Wrong Loopback0 ip prefix: '%s'" % ip_addr_w_mask)
                return True
            ip_addr = ip_addr_w_mask[:slash_pos]
            if TemplateFabric.is_ipv4(ip_addr):
                attr, rm_name, ip_proto = "lo_ipv4", "RM_SET_SRC", ""
            elif TemplateFabric.is_ipv6(ip_addr):
                attr, rm_name, ip_proto = "lo_ipv6", "RM_SET_SRC6", "v6"
            else:
                log_err("Got ambiguous ip address '%s'" % ip_addr)
                return True

            if getattr(self, attr) == ip_addr:
                # A prefix-length change can move the same source to a new key.
                setattr(self, attr + "_key", key)
                return True
            try:
                txt = self.zebra_set_src_template.render(
                    rm_name=rm_name, lo_ip=ip_addr, ip_proto=ip_proto)
            except jinja2.TemplateError as e:
                log_err("Error while rendering 'set src' template: %s" % str(e))
                return True
            self.cfg_mgr.push(txt)
            setattr(self, attr, ip_addr)
            setattr(self, attr + "_key", key)
            log_info("The 'set src' configuration with Loopback0 ip '%s' has been scheduled to be added" % ip_addr)
        return True

    def del_handler(self, key):
        """ Implementation of 'DEL' command for this class """
        self.directory.remove(self.db_name, self.table_name, key)
        for attr, protocol, rm_name in (
                ("lo_ipv4", "ip", "RM_SET_SRC"),
                ("lo_ipv6", "ipv6", "RM_SET_SRC6")):
            if key != getattr(self, attr + "_key"):
                continue
            # Detach first so BGP routes no longer use the removed source.
            self.cfg_mgr.push("no %s protocol bgp route-map %s\nno route-map %s" %
                              (protocol, rm_name, rm_name))
            setattr(self, attr, None)
            setattr(self, attr + "_key", None)
            log_info("The 'set src' configuration for '%s' has been scheduled to be removed" % key)
