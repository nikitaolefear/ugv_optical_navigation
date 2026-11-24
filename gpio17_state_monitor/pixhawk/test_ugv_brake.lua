-- Run from the candidate directory with Lua 5.3+.
-- No ArduPilot, serial or PWM hardware is used.
local now, pwm, rc_valid, queue = 0, 1000, true, {}
local left, right
local config = {
    UGVB_LINK = 0, UGVB_CH_APPLY = 2000, UGVB_CH_RELEASE = 1000,
    UGVB_L_APPLY = 1800, UGVB_L_RELEASE = 1200,
    UGVB_R_APPLY = 1200, UGVB_R_RELEASE = 1800, UGVB_STEP = 300,
    SERVO5_MIN = 1000, SERVO5_MAX = 2000, SERVO5_FUNCTION = 94,
    SERVO6_MIN = 1000, SERVO6_MAX = 2000, SERVO6_FUNCTION = 95, RC_OPTIONS = 2,
}
param = {add_table = function() return true end, add_param = function() return true end,
         get = function(_, name) return config[name] or 0 end}
rc = {has_valid_input = function() return rc_valid end,
      get_pwm = function(_, channel) assert(channel == 8); return pwm end}
mavlink = {init = function() queue = {} end,
           register_rx_msgid = function(_, id) assert(id == 252) end,
           receive_chan = function() return table.unpack(table.remove(queue, 1) or {}) end}
SRV_Channels = {set_output_pwm = function(_, fn, value)
    if fn == 94 then left = value elseif fn == 95 then right = value else error("wrong output") end
end}
local meta = {}
local function stamp(value) return setmetatable({value = value}, meta) end
meta.__sub = function(a, b) return stamp(a.value - b.value) end
meta.__index = {toint = function(self) return self.value end}
millis = function() return stamp(now) end

local function rx(state, channel, age, system)
    local message = string.pack("<I2BBBBBBBI3I4i4c10", 0, 253, 18, 0, 0, 0,
                                system or 255, 190, 252, now, state, "UGVBRAKE03")
    queue[#queue + 1] = {message .. string.rep("\0", 291 - #message), channel or 0, stamp(now - (age or 0))}
end
local function read(l, r) assert(left == l and right == r, "Unexpected PWM at line " .. debug.getinfo(2, "l").currentline .. ": " .. left .. "/" .. right) end

local update = dofile("pixhawk/ugv_brake.lua")
local function tick(elapsed) now = now + (elapsed or 50); update() end
read(1800, 1200)
rx(2); tick(); read(1800, 1200)  -- RESET requires a prior HOLD on this Lua run.
rx(0); tick()
rx(2, 1); rx(2, 0, 400); rx(2, 0, 0, 42)
tick(); read(1800, 1200)
pwm = 2000; rx(2); tick(); read(1800, 1200)  -- Manual APPLY overrides RESET.
pwm = 1000; rx(1); tick(); read(1500, 1500)  -- Manual control after RESET.
tick(); read(1200, 1800)
pwm = 2000; tick(); tick(); read(1800, 1200)
pwm = 1000; tick(); tick(); read(1200, 1800)
rx(0); tick(); tick(); read(1800, 1200)
rx(2); tick(); read(1500, 1500)  -- RESET releases when CH8 is already in RELEASE.
rx(1); tick(); read(1200, 1800)
rx(0); tick(); read(1500, 1500)  -- STOP interrupts release.
tick(); read(1800, 1200)
rx(2); tick(); rx(1); tick(); read(1200, 1800)
rc_valid = false; tick(); tick(); read(1800, 1200)
rc_valid = true; rx(1); tick(); read(1800, 1200)
rx(0); tick(); rx(2); tick(); tick(); read(1200, 1800)
tick(400); tick(); read(1800, 1200)
update = dofile("pixhawk/ugv_brake.lua")
rx(1); tick(); read(1800, 1200)
config.SERVO5_FUNCTION = 58
assert(not pcall(dofile, "pixhawk/ugv_brake.lua"))
config.SERVO5_FUNCTION = 94

-- Measured CH8 points are valid, but the measured raw outputs exceed the baseline.
config.UGVB_CH_APPLY, config.UGVB_CH_RELEASE = 999, 2000
config.SERVO5_MAX, config.SERVO6_MAX = 1900, 1900
assert(pcall(dofile, "pixhawk/ugv_brake.lua"))
for _, endpoint in ipairs({"L_APPLY", "L_RELEASE", "R_APPLY", "R_RELEASE"}) do
    local name = "UGVB_" .. endpoint
    local original = config[name]
    config[name] = endpoint:find("APPLY") and 999 or 2000
    local ok, err = pcall(dofile, "pixhawk/ugv_brake.lua")
    assert(not ok and err:find("Invalid brake endpoints", 1, true), endpoint)
    config[name] = original
end
-- Actual bench endpoints with the proposed bounds and original reversals.
config.SERVO5_REVERSED, config.SERVO6_REVERSED = 0, 1
config.SERVO5_MIN, config.SERVO6_MIN = 999, 999
config.SERVO5_MAX, config.SERVO6_MAX = 2000, 2000
config.UGVB_L_APPLY, config.UGVB_R_APPLY = 999, 999
config.UGVB_L_RELEASE, config.UGVB_R_RELEASE = 2000, 2000
config.UGVB_STEP = 300 -- Test fixture, not a bench ramp recommendation.
local function settle()
    for _ = 1, 4 do tick() end
end
pwm = 2000
update = dofile("pixhawk/ugv_brake.lua")
read(999, 999)
rx(0); tick()
rx(2); settle(); read(2000, 2000)
pwm = 999; rx(1); settle(); read(999, 999)
pwm = 2000; rx(1); settle(); read(2000, 2000)
rx(0); settle(); read(999, 999)
rx(2); settle(); read(2000, 2000)
rc_valid = false; settle(); read(999, 999)
rc_valid = true; rx(1); settle(); read(999, 999)
rx(0); tick(); tick(350)
rx(2); settle(); read(999, 999) -- Expired HOLD cannot authorize RESET.
rx(0); tick()
rx(2); settle(); read(2000, 2000)
tick(350); settle(); read(999, 999) -- Owner lease lost.
rx(0); tick()
rx(2); settle(); read(2000, 2000)
update = dofile("pixhawk/ugv_brake.lua")
rx(1); settle(); read(999, 999) -- Restart requires a new HOLD and RESET.
config.SERVO6_MAX, config.UGVB_R_RELEASE = 2100, 2100
local ok, err = pcall(dofile, "pixhawk/ugv_brake.lua")
assert(not ok and err:find("Invalid brake endpoints", 1, true))
config.SERVO6_MAX, config.UGVB_R_RELEASE = 2000, 2000
config.UGVB_APPLY_ONLY = 1
config.UGVB_LINK, config.UGVB_STEP = -1, -1
local mavlink_mock = mavlink
mavlink = nil -- The isolated mode must not use the owner link.
local armed = true
arming = {is_armed = function() return armed end}
left, right = nil, nil
assert(dofile("pixhawk/ugv_brake.lua") == nil and left == nil and right == nil)
armed = false
pwm = 2000
update = dofile("pixhawk/ugv_brake.lua")
read(999, 999)
pwm = 999; tick(); read(999, 999)
pwm = 2000; rc_valid = false; tick(); read(999, 999)
armed = true
left, right = nil, nil
assert(update() == nil and left == nil and right == nil)
armed = false
config.UGVB_L_APPLY = 1000
assert(not pcall(dofile, "pixhawk/ugv_brake.lua"))

config.UGVB_L_APPLY = 999
config.UGVB_LINK, config.UGVB_STEP = 2, 300
mavlink = mavlink_mock
local reports = {}
gcs = {send_text = function(_, severity, text)
    assert(severity == 6)
    reports[#reports + 1] = text
end}
local output = SRV_Channels.set_output_pwm
SRV_Channels.set_output_pwm = function(self, fn, value)
    assert(value == 999, "HOLD-only handoff emitted a non-APPLY output")
    output(self, fn, value)
end
rc_valid, pwm = true, 2000
update = dofile("pixhawk/ugv_brake.lua")
local function advance()
    now = now + 50
    local next_update, delay = update()
    assert(delay == 50)
    update = next_update
    read(999, 999)
end
rx(0, 1); rx(0, 2, 400); rx(0, 2, 0, 42); rx(2, 2)
advance(); assert(#reports == 0)
local sent_ms = now
rx(0, 2); advance()
assert(reports[1] == "UGVB_HOLD_RX APPLY_ONLY " .. sent_ms)
config.UGVB_APPLY_ONLY = 0
local apply = update
rx(0, 2); advance(); assert(update == apply) -- CH8 remains in RELEASE.
pwm = 999
advance(); assert(update == apply) -- A fresh packet must arrive on the handoff tick.
rx(0, 2); rx(2, 2); advance(); assert(update ~= apply)
assert(#reports == 1)
sent_ms = now
rx(0, 2); advance()
assert(reports[2] == "UGVB_HOLD_RX FUNCTIONAL " .. sent_ms)
pwm = 2000
rx(2, 2); rx(1, 2); advance()
config.UGVB_APPLY_ONLY = 1
advance(); assert(update == apply)
print("Pixhawk functional brake arbitration: OK")
