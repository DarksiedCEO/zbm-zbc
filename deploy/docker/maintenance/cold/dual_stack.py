"""Explicit dual-stack wildcard listeners; loopback IPv4 fixtures stay IPv4."""
import socket

class DualStackMixin:
 def server_bind(self):
  if self.address_family == socket.AF_INET6:
   self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
  return super().server_bind()
 def __init__(self, address, *args, **kwargs):
  if ':' in address[0]:
   self.address_family = socket.AF_INET6
  super().__init__(address, *args, **kwargs)
