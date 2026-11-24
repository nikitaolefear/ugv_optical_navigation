-- Temporary brake-cycle profile for a physically isolated stand.
-- Install in place of ugv_brake.lua only for the approved bench session.
local HOLD, KEEP, RELEASE = 0, 1, 2
local period, lease, window = 50, 350, 3000
local key = 87
assert(param:add_table(key, "UGVB_", 9), "UGV brake parameter table")
local names = {"LINK", "CH_APPLY", "CH_RELEASE", "L_APPLY", "L_RELEASE", "R_APPLY", "R_RELEASE", "STEP"}
for i, name in ipairs(names) do
    assert(param:add_param(key, i, name, -1), "UGV brake parameter")
end
assert(param:add_param(key, 9, "APPLY_ONLY", 0), "UGV brake parameter")
assert(param:get("UGVB_APPLY_ONLY") == 1, "Bench profile requires rollback APPLY_ONLY=1")
assert(param:get("UGVB_LINK") == 2 and param:get("UGVB_STEP") == 300, "Bench link and ramp must match the approved values")
assert(param:get("UGVB_L_APPLY") == 999 and param:get("UGVB_R_APPLY") == 999
       and param:get("UGVB_L_RELEASE") == 2000 and param:get("UGVB_R_RELEASE") == 2000,
       "Bench endpoints must be 999 and 2000 on both outputs")
for i = 1, 16 do
    local prefix = "SERVO" .. i .. "_"
    local fn = param:get(prefix .. "FUNCTION")
    if i == 5 or i == 6 then
        assert(fn == i + 89 and param:get(prefix .. "MIN") == 999 and param:get(prefix .. "MAX") == 2000,
               "Invalid bench output configuration")
    else
        assert(fn ~= 94 and fn ~= 95, "Brake function assigned to another output")
    end
end
assert((math.floor(param:get("RC_OPTIONS") or 0) & 2) ~= 0, "Disable MAVLink RC overrides for trusted CH8")

-- Same MAVLink 2 message and source as the normal arbiter, with a distinct
-- bench tag so production HOLD/KEEP/RESET_RELEASE cannot authorize this test.
mavlink:init(4, 1)
mavlink:register_rx_msgid(252)
local function bench_packet(message)
    if #message ~= 291 and #message ~= 298 then return nil end
    local magic, length = string.unpack("<BB", message, 3)
    if magic ~= 253 or length ~= 18 then return nil end
    local sys, comp, id, payload
    if #message == 291 then
        sys, comp, id, payload = string.unpack("<BBI3", message, 8)
    else
        sys, comp, id, payload = string.unpack("<I4BI3", message, 8)
    end
    if sys ~= 255 or comp ~= 190 or id ~= 252 then return nil end
    local sent_ms, state, name = string.unpack("<I4i4c10", message, payload)
    if name == "UGVBENCH01" then return state, sent_ms end
end

local output = 999
local last_hold, release_started, last_permit, last_ready
local apply_seen, consumed = false, false
local function before_limit(now, stamp, limit)
    if not stamp then return false end
    local age = (now - stamp):toint()
    return age >= 0 and age < limit
end

local function report(text)
    pcall(function() gcs:send_text(6, text) end)
end

local function revoke(reason, hold_ms)
    if release_started then
        local detail = hold_ms and string.format(" %u", hold_ms) or ""
        report("UGVB_BENCH_APPLY_REQ " .. reason .. detail)
    end
    release_started, last_permit = nil, nil
end

local function update()
    if arming:is_armed() then
        if release_started then report("UGVB_BENCH_ABORT ARMED") end
        release_started, last_permit = nil, nil
        consumed, output = true, 999
        return update, period
    end
    local now = millis()
    local pwm = rc:has_valid_input() and rc:get_pwm(8)
    local channel_valid = type(pwm) == "number" and pwm >= 800 and pwm <= 2200
    local manual_apply = channel_valid and math.abs(pwm - 999) <= 75
    local manual_release = channel_valid and math.abs(pwm - 2000) <= 75
    if not channel_valid or not before_limit(now, last_hold, lease) then
        last_hold, apply_seen = nil, false
    end
    if release_started then
        if not manual_release then revoke("RC8")
        elseif not before_limit(now, last_permit, lease) then revoke("LINK")
        elseif not before_limit(now, release_started, window) then revoke("TIMEOUT") end
    end

    local hold, keep, release
    local hold_ms, release_ms
    for _ = 1, 4 do
        local bytes, channel, received = mavlink:receive_chan()
        if not bytes then break end
        if channel == 2 and before_limit(now, received, lease) then
            local state, sent_ms = bench_packet(bytes)
            if state == HOLD then hold, hold_ms = received, sent_ms
            elseif state == KEEP then keep = received
            elseif state == RELEASE then release, release_ms = received, sent_ms end
        end
    end
    if hold then
        revoke("HOLD", hold_ms)
        last_hold = hold
        apply_seen = channel_valid and (apply_seen or manual_apply)
        if manual_apply and not consumed and not before_limit(now, last_ready, 1000) then
            report(string.format("UGVB_BENCH_READY %u", hold_ms))
            last_ready = now
        end
    end
    if release and not consumed then
        consumed = true
        if not hold and apply_seen and manual_release and before_limit(release, last_hold, lease) then
            release_started, last_permit = release, release
            report(string.format("UGVB_BENCH_RELEASE %u", release_ms))
        else
            report("UGVB_BENCH_APPLY_REQ RELEASE_DENIED")
        end
    elseif release_started and keep and before_limit(now, keep, lease)
           and (keep - last_permit):toint() >= 0 then
        last_permit = keep
    end

    local target = release_started and 2000 or 999
    output = output + math.max(-300, math.min(300, target - output))
    SRV_Channels:set_output_pwm(94, output)
    SRV_Channels:set_output_pwm(95, output)
    return update, period
end

return update()
