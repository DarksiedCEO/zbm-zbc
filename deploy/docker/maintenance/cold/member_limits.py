"""Finite Mac profile based on measured day-30 store sizes; not platform sizing."""
MIB=1048576
MEMBERS=('ledger','finance','legal','compliance','seo')
def limits(member):
 if member not in MEMBERS:raise ValueError('fixed member profile')
 large=member in ('ledger','compliance')
 return {'source':2*MIB if large else MIB//2,
         'stage':3*MIB if large else MIB,
         'repository':4*MIB if large else 2*MIB,
         'control_reserve':MIB} # 512KiB owner journal plus atomic replacement
