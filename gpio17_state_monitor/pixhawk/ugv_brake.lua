-- Functional brake arbitration. Install only in the separately approved
-- brake profile. Script outputs 94/95 must belong exclusively to 5/6.
local HOLD, KEEP, RESET_RELEASE = 0, 1, 2
local period, lease = 50, 350
local KEY = 87
assert(param:add_table(KEY, "UGVB_", 9), "UGV brake parameter table")
local names = {"LINK", "CH_APPLY", "CH_RELEASE", "L_APPLY", "L_RELEASE", "R_APPLY", "R_RELEASE", "STEP"}
for i, name in ipairs(names) do
    assert(param:add_param(KEY, i, name, -1), "UGV brake parameter")
end
assert(param:add_param(KEY, 9, "APPLY_ONLY", 0), "UGV bench APPLY-only parameter")

local function configured(name)
    local value = param:get("UGVB_" .. name)
    assert(value and value >= 0 and value % 1 == 0, "Configure UGVB_" .. name)
    return value
end

local function endpoints(channel, suffix)
    local apply, release = configured(suffix .. "_APPLY"), configured(suffix .. "_RELEASE")
    local prefix = "SERVO" .. channel .. "_"
    local lo, hi = param:get(prefix .. "MIN"), param:get(prefix .. "MAX")
    assert(lo and hi and lo >= 800 and hi <= 2200
           and apply >= math.max(lo, 999) and apply <= math.min(hi, 2000)
           and release >= math.max(lo, 999) and release <= math.min(hi, 2000)
           and apply ~= release, "Invalid brake endpoints on output " .. channel)
    return apply, release
end
local la, lr = endpoints(5, "L")
local ra, rr = endpoints(6, "R")
for index = 1, 16 do
    local fn = param:get("SERVO" .. index .. "_FUNCTION")
    if index == 5 or index == 6 then
        assert(fn == index + 89, "Brake output is not assigned exclusively to Lua")
    else
        assert(fn ~= 94 and fn ~= 95, "Brake function assigned to another output")
    end
end
assert((math.floor(param:get("RC_OPTIONS") or 0) & 2) ~= 0, "Disable MAVLink RC overrides for trusted CH8")

-- ArduPilot passes a parsed, CRC-checked mavlink_message_t, not a wire frame.
-- Accept only the fixed MAVLink 2 NAMED_VALUE_INT packet on the dedicated link.
local function gate_packet(message)
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
    local sent_ms, value, name = string.unpack("<I4i4c10", message, payload)
    if name ~= "UGVBRAKE03" or (value ~= HOLD and value ~= KEEP and value ~= RESET_RELEASE) then return nil end
    return value, sent_ms
end

local function init_gate()
    mavlink:init(4, 1)
    mavlink:register_rx_msgid(252)
end

local function functional_update(link, report_hold, fallback)
    local ch_apply, ch_release = configured("CH_APPLY"), configured("CH_RELEASE")
    local step = configured("STEP")
    assert(link <= 31 and step > 0 and step <= 1000, "Invalid brake link or ramp")
    assert(ch_apply >= 800 and ch_apply <= 2200 and ch_release >= 800 and ch_release <= 2200
           and math.abs(ch_apply - ch_release) >= 200, "Invalid CH8 switch points")
    local last_permit, last_hold, authorized = nil, nil, false
    local left, right = la, ra
    local function ramp(value, target)
        return value + math.max(-step, math.min(step, target - value))
    end
    local function around(pwm, point)
        return math.abs(pwm - point) <= 75
    end

    local function update()
        if fallback then
            if arming:is_armed() then return end
            if param:get("UGVB_APPLY_ONLY") ~= 0 then return fallback() end
        end
        local now = millis()
        if last_permit and (now - last_permit):toint() >= lease then
            last_permit, last_hold, authorized = nil, nil, false
        end
        local pwm = rc:has_valid_input() and rc:get_pwm(8)
        local manual_release = type(pwm) == "number" and not around(pwm, ch_apply) and around(pwm, ch_release)
        for _ = 1, 4 do
            local bytes, channel, received = mavlink:receive_chan()
            if not bytes then break end
            local age = received and (now - received):toint()
            if channel == link and age and age >= 0 and age < lease then
                local state, sent_ms = gate_packet(bytes)
                if state == HOLD then
                    last_permit, last_hold, authorized = nil, received, false
                    if report_hold then report_hold(now, "FUNCTIONAL", sent_ms) end
                elseif not fallback and state == RESET_RELEASE then
                    local hold_age = last_hold and (received - last_hold):toint()
                    authorized = hold_age ~= nil and hold_age >= 0 and hold_age < lease
                                 and type(pwm) == "number"
                    last_hold = nil
                    last_permit = authorized and received or nil
                elseif not fallback and state == KEEP and last_permit then
                    last_permit = received
                end
            end
        end

        if type(pwm) ~= "number" then
            last_permit, last_hold, authorized = nil, nil, false
        end
        local released = authorized and manual_release
        left = ramp(left, released and lr or la)
        right = ramp(right, released and rr or ra)
        SRV_Channels:set_output_pwm(94, left)
        SRV_Channels:set_output_pwm(95, right)
        return update, period
    end
    return update
end

-- The staged in-process handoff remains HOLD-only until this Lua run ends.
-- A configured LINK opts the APPLY-only branch into receive-only diagnostics.
local apply_only = param:get("UGVB_APPLY_ONLY")
assert(apply_only == 0 or apply_only == 1, "Invalid UGVB_APPLY_ONLY")
if apply_only == 1 then
    assert(la == 999 and ra == 999, "Bench APPLY must be 999 on both outputs")
    local link = param:get("UGVB_LINK")
    local receiver = type(link) == "number" and link % 1 == 0 and link >= 0 and link <= 31
                     and pcall(init_gate)
    local last_report, last_phase
    local function report_hold(now, phase, sent_ms)
        if last_phase == phase and (now - last_report):toint() < 1000 then return true end
        local ok = pcall(function()
            gcs:send_text(6, string.format("UGVB_HOLD_RX %s %u", phase, sent_ms))
        end)
        if ok then last_report, last_phase = now, phase end
        return ok
    end
    local function handoff(apply)
        local now = millis()
        local reported_apply_hold = last_phase == "APPLY_ONLY"
        local fresh_hold = false
        for _ = 1, 4 do
            local bytes, channel, received = mavlink:receive_chan()
            if not bytes then break end
            local age = received and (now - received):toint()
            if channel == link and age and age >= 0 and age < lease then
                local state, sent_ms = gate_packet(bytes)
                if state == HOLD and report_hold(now, "APPLY_ONLY", sent_ms) then
                    fresh_hold = true
                end
            end
        end
        if reported_apply_hold and fresh_hold and param:get("UGVB_APPLY_ONLY") == 0
           and rc:has_valid_input() then
            local pwm = rc:get_pwm(8)
            local apply_point = param:get("UGVB_CH_APPLY")
            if type(pwm) == "number" and type(apply_point) == "number" and
               math.abs(pwm - apply_point) <= 75 then
                return functional_update(link, report_hold, apply)
            end
        end
    end
    local function apply()
        if arming:is_armed() then return end
        SRV_Channels:set_output_pwm(94, la)
        SRV_Channels:set_output_pwm(95, ra)
        if receiver then
            local ok, update = pcall(handoff, apply)
            if ok and update then return update, period end
        end
        return apply, period
    end
    return apply()
end
local link = configured("LINK")
local update = functional_update(link)
init_gate()
return update()
