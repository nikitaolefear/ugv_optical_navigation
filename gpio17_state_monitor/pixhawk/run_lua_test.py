"""Execute the offline Lua test with a system Lua 5.4 shared library."""
import ctypes as c
from ctypes.util import find_library
from pathlib import Path
import os

library = find_library('lua5.4')
if library is None:
    raise SystemExit('Lua 5.4 is required (or run test_ugv_brake.lua with lua5.3+)')
lua = c.CDLL(library)
pointer = c.c_void_p
for name, args, result in (
    ('luaL_newstate', (), pointer),
    ('luaL_openlibs', (pointer,), None),
    ('luaL_loadfilex', (pointer, c.c_char_p, c.c_char_p), c.c_int),
    ('lua_pcallk', (pointer, c.c_int, c.c_int, c.c_int, c.c_int64, pointer), c.c_int),
    ('lua_tolstring', (pointer, c.c_int, pointer), c.c_char_p),
    ('lua_close', (pointer,), None),
):
    function = getattr(lua, name)
    function.argtypes = args
    function.restype = result

os.chdir(Path(__file__).resolve().parents[1])
state = lua.luaL_newstate()
if not state:
    raise SystemExit('Could not allocate Lua state')
try:
    lua.luaL_openlibs(state)
    if (lua.luaL_loadfilex(state, b'pixhawk/test_ugv_brake.lua', None) or
            lua.lua_pcallk(state, 0, 0, 0, 0, None)):
        raise SystemExit(lua.lua_tolstring(state, -1, None).decode())
finally:
    lua.lua_close(state)
