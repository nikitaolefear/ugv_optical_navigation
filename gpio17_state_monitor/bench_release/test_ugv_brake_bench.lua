-- Run from gpio17_stop_candidate: lua bench_release/test_ugv_brake_bench.lua
local hold, keep, release = 0, 1, 2
local now, pwm, valid, armed, queue, reports, outputs, writes
local params = {
    UGVB_LINK = 2, UGVB_STEP = 300, UGVB_APPLY_ONLY = 1,
    UGVB_L_APPLY = 999, UGVB_R_APPLY = 999,
    UGVB_L_RELEASE = 2000, UGVB_R_RELEASE = 2000,
    SERVO5_FUNCTION = 94, SERVO6_FUNCTION = 95,
    SERVO5_MIN = 999, SERVO6_MIN = 999, SERVO5_MAX = 2000, SERVO6_MAX = 2000,
    RC_OPTIONS = 34,
}
param = {
    add_table = function() return true end,
    add_param = function() return true end,
    get = function(_, name) return params[name] or 0 end,
}
rc = {
    has_valid_input = function() return valid end,
    get_pwm = function(_, channel) assert(channel == 8); return pwm end,
}
arming = {is_armed = function() return armed end}
mavlink = {
    init = function(_, depth, ids) assert(depth == 4 and ids == 1); queue = {} end,
    register_rx_msgid = function(_, id) assert(id == 252) end,
    receive_chan = function() return table.unpack(table.remove(queue, 1) or {}) end,
}
gcs = {send_text = function(_, severity, text)
    assert(severity == 6)
    reports[#reports + 1] = text
end}
SRV_Channels = {set_output_pwm = function(_, channel, value)
    assert((channel == 94 or channel == 95) and value >= 999 and value <= 2000)
    outputs[channel] = value
    writes = writes + 1
end}
local meta = {}
local function stamp(value) return setmetatable({value = value}, meta) end
meta.__sub = function(a, b) return stamp(a.value - b.value) end
meta.__index = {toint = function(self) return self.value end}
millis = function() return stamp(now) end
local function rx(state, opts)
    opts = opts or {}
    local id, name = 252, opts.name or "UGVBENCH01"
    local system, component = opts.system or 255, opts.component or 190
    local layout = opts.layout or 291
    local header = string.pack("<I2BBBBB", 0, 253, 18, 0, 0, 0)
    local sender = layout == 291 and string.pack("<BBI3", system, component, id)
                                 or string.pack("<I4BI3", system, component, id)
    local message = header .. sender .. string.pack("<I4i4c10", now, state, name)
    queue[#queue + 1] = {message .. string.rep("\0", layout - #message),
                          opts.channel or 2, stamp(now - (opts.age or 0))}
end
local function expect(value)
    assert(outputs[94] == value and outputs[95] == value,
           ("Expected %d/ %d, got %s/ %s"):format(value, value, tostring(outputs[94]), tostring(outputs[95])))
end
local update
local function load()
    now, pwm, valid, armed, queue, reports, outputs, writes = 0, 999, true, false, {}, {}, {}, 0
    local delay
    update, delay = dofile("bench_release/ugv_brake_bench.lua")
    assert(delay == 50)
    expect(999)
end
local function tick(delay)
    now = now + (delay or 50)
    local next_update, next_delay = update()
    assert(next_update == update and next_delay == 50)
end
local function start_release()
    rx(hold); tick()
    assert(reports[#reports]:match("^UGVB_BENCH_READY "))
    pwm = 2000
    rx(release, {layout = 298}); tick()
    assert(reports[#reports]:match("^UGVB_BENCH_RELEASE "))
    expect(1299)
    for _ = 1, 3 do rx(keep); tick() end
    expect(2000)
end

load()
rx(hold); tick()
pwm = 2000
rx(release, {name = "UGVBRAKE03"}); rx(release, {system = 42}); rx(release, {channel = 3})
rx(release, {component = 1}); tick(); expect(999)
rx(release, {age = 350}); tick(); expect(999)
rx(release); tick(); expect(1299) -- Rejections above left the one-shot unused.

for _, order in ipairs({{hold, release}, {release, hold}}) do
    load()
    rx(hold); tick()
    pwm = 2000
    rx(order[1]); rx(order[2]); tick(); expect(999)
    rx(release); tick(); expect(999)
end

load()
pwm = 2000
rx(hold); tick()
rx(release); tick(); expect(999) -- A HOLD alone cannot prove CH8 APPLY.

load()
rx(hold); tick()
pwm = 2000
tick(350)
rx(hold); tick()
rx(release); tick(); expect(999) -- The prior APPLY evidence aged out with the HOLD stream.

load()
start_release()
local stop_ms = now
rx(keep); rx(hold); rx(release); tick(); expect(1700)
assert(reports[#reports] == ("UGVB_BENCH_APPLY_REQ HOLD %u"):format(stop_ms))
for _ = 1, 3 do rx(keep); tick() end
expect(999)
rx(hold); tick()
rx(release); tick(); expect(999) -- STOP consumes the attempt permanently.

load()
start_release()
tick(350); expect(1700)
for _ = 1, 3 do rx(keep); tick() end
expect(999)
assert(reports[#reports] == "UGVB_BENCH_APPLY_REQ LINK")

load()
start_release()
valid = false
rx(keep); tick(); expect(1700)
valid = true
for _ = 1, 3 do rx(keep); tick() end
expect(999)
assert(reports[#reports] == "UGVB_BENCH_APPLY_REQ RC8")

load()
start_release()
local before = writes
armed = true
rx(keep); tick(); assert(writes == before)
armed = false
rx(keep); tick(); expect(999)
assert(reports[#reports] == "UGVB_BENCH_ABORT ARMED")

load()
start_release()
while now < 3050 do rx(keep); tick() end
assert(reports[#reports] == "UGVB_BENCH_APPLY_REQ TIMEOUT")
expect(1700)
for _ = 1, 3 do rx(keep); tick() end
expect(999)

load()
start_release()
update = dofile("bench_release/ugv_brake_bench.lua")
expect(999)
rx(keep); tick(); expect(999) -- A restarted Lua cannot act on KEEP alone.
params.UGVB_APPLY_ONLY = 0
assert(not pcall(dofile, "bench_release/ugv_brake_bench.lua"))
print("Bench one-shot Lua: channel gates, STOP dominance, lease, timeout and restart OK")
