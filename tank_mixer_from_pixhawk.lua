-- Rover Tank Mixer for ArduPilot Rover 4.6.x
-- 
-- What it does:
--   * RC3 = throttle
--   * RC1 = steering
--   * In MANUAL mode only, it mixes the sticks into 4 outputs:
--       OUT1 / OUT3 = left side
--       OUT2 / OUT4 = right side
--   * Disarmed or non-MANUAL modes go to neutral (1500 us)
--
-- Setup required in Mission Planner:
--   * SCR_ENABLE = 1
--   * Put this file in APM/scripts/
--   * Assign the four motor outputs to scripting outputs:
--       SERVO1_FUNCTION = 94
--       SERVO2_FUNCTION = 95
--       SERVO3_FUNCTION = 96
--       SERVO4_FUNCTION = 97
--   * If you need more script memory, increase SCR_HEAP_SIZE

local OUT_FUNCS = {94, 95, 96, 97}

local RC_STEER_CH = 1
local RC_THROTTLE_CH = 3

local PWM_NEUTRAL = 1500
local PWM_MIN = 1000
local PWM_MAX = 2000

-- Script parameters (Mission Planner: SCR_USER1..SCR_USER6)
--   SCR_USER1 = steering expo x100        (default 170 = 1.70)
--   SCR_USER2 = pivot zone percent        (default 18 = 18%)
--   SCR_USER3 = inner-side minimum percent (default 8  = 8%)
--   SCR_USER4 = slew limit us per update   (default 20)
--   SCR_USER5 = input deadband us          (default 20)
--   SCR_USER6 = timeout ms                 (default 120)
local function get_param(name, default_value)
    local v = param:get(name)
    if v == nil then
        return default_value
    end
    return v
end

local function clamp(v, lo, hi)
    if v < lo then
        return lo
    elseif v > hi then
        return hi
    end
    return v
end

local function sign(v)
    if v < 0 then
        return -1
    elseif v > 0 then
        return 1
    end
    return 0
end

local function smoothstep(x)
    x = clamp(x, 0, 1)
    return x * x * (3 - 2 * x)
end

local function expo(v, expo_value)
    local a = math.abs(v)
    return sign(v) * (a ^ expo_value)
end

local function pwm_to_norm(pwm, trim, min_pwm, max_pwm, deadband_us)
    if pwm == nil then
        return 0
    end

    local d = math.abs(pwm - trim)
    if d <= deadband_us then
        return 0
    end

    if pwm > trim then
        local span = max_pwm - trim - deadband_us
        if span <= 0 then
            return 0
        end
        return clamp((pwm - trim - deadband_us) / span, 0, 1)
    else
        local span = trim - min_pwm - deadband_us
        if span <= 0 then
            return 0
        end
        return -clamp((trim - pwm - deadband_us) / span, 0, 1)
    end
end

local function norm_to_pwm(v)
    v = clamp(v, -1, 1)
    if v >= 0 then
        return math.floor(PWM_NEUTRAL + (PWM_MAX - PWM_NEUTRAL) * v + 0.5)
    else
        return math.floor(PWM_NEUTRAL + (PWM_NEUTRAL - PWM_MIN) * v + 0.5)
    end
end

local function slew_limit(target, current, max_step)
    if target > current + max_step then
        return current + max_step
    elseif target < current - max_step then
        return current - max_step
    end
    return target
end

local output_chans = nil
local last_pwm = {PWM_NEUTRAL, PWM_NEUTRAL, PWM_NEUTRAL, PWM_NEUTRAL}

local function init_outputs()
    local chans = {}
    for i = 1, 4 do
        local ch = SRV_Channels:find_channel(OUT_FUNCS[i])
        if not ch then
            return nil, "SERVO function " .. tostring(OUT_FUNCS[i]) .. " is not assigned"
        end
        chans[i] = ch
    end
    return chans, nil
end

local function mix(throttle, steer, pivot_zone, inner_min)
    -- Steering curve: make small stick movements gentle.
    steer = expo(steer, math.max(1.0, get_param("SCR_USER1", 170) / 100.0))

    local abs_t = math.abs(throttle)
    local pivot_w = 0
    if pivot_zone > 0 then
        pivot_w = smoothstep(1 - (abs_t / pivot_zone))
    end

    -- Pivot mix (tank turn around center)
    local left_pivot = throttle + steer
    local right_pivot = throttle - steer

    -- Smooth curve mix (inner side slows down but does not reverse)
    local s = sign(steer)
    local steer_abs = math.abs(steer)
    local inner_factor = 1 - steer_abs * (1 - inner_min)
    inner_factor = clamp(inner_factor, inner_min, 1)

    local left_curve, right_curve
    if s >= 0 then
        -- steer right: left side is outer, right side is inner
        left_curve = throttle
        right_curve = throttle * inner_factor
    else
        -- steer left: right side is outer, left side is inner
        left_curve = throttle * inner_factor
        right_curve = throttle
    end

    local left = left_pivot * pivot_w + left_curve * (1 - pivot_w)
    local right = right_pivot * pivot_w + right_curve * (1 - pivot_w)

    -- Keep within [-1, 1]
    local m = math.max(math.abs(left), math.abs(right))
    if m > 1 then
        left = left / m
        right = right / m
    end

    return left, right
end

local function apply_outputs(left_pwm, right_pwm, timeout_ms, slew_us)
    local targets = {
        left_pwm,
        right_pwm,
        left_pwm,
        right_pwm
    }

    for i = 1, 4 do
        last_pwm[i] = slew_limit(targets[i], last_pwm[i], slew_us)
        SRV_Channels:set_output_pwm_chan_timeout(output_chans[i], last_pwm[i], timeout_ms)
    end
end

function update()
    if output_chans == nil then
        local chans, err = init_outputs()
        if chans == nil then
            gcs:send_text(4, "tank_mixer: " .. tostring(err))
            return update, 1000
        end
        output_chans = chans
        gcs:send_text(6, "tank_mixer: outputs ready")
    end

    local timeout_ms = get_param("SCR_USER6", 120)
    local slew_us = get_param("SCR_USER4", 20)
    local deadband_us = get_param("SCR_USER5", 20)
    local pivot_zone = get_param("SCR_USER2", 18) / 100.0
    local inner_min = get_param("SCR_USER3", 8) / 100.0

    local armed = arming:is_armed()
    local mode = vehicle:get_mode()

    local left_pwm = PWM_NEUTRAL
    local right_pwm = PWM_NEUTRAL

    -- Rover MANUAL is mode 0 in current ArduPilot source.
    -- In every other mode, stay neutral so the normal Rover logic keeps control.
    if armed and mode == 0 then
        local steer_pwm = rc:get_pwm(RC_STEER_CH)
        local thr_pwm = rc:get_pwm(RC_THROTTLE_CH)

        local steer_trim = get_param("RC1_TRIM", 1500)
        local steer_min = get_param("RC1_MIN", 1000)
        local steer_max = get_param("RC1_MAX", 2000)

        local thr_trim = get_param("RC3_TRIM", 1500)
        local thr_min = get_param("RC3_MIN", 1000)
        local thr_max = get_param("RC3_MAX", 2000)

        local steer_n = pwm_to_norm(steer_pwm, steer_trim, steer_min, steer_max, deadband_us)
        local thr_n = pwm_to_norm(thr_pwm, thr_trim, thr_min, thr_max, deadband_us)

        local left_n, right_n = mix(thr_n, steer_n, pivot_zone, inner_min)

        left_pwm = norm_to_pwm(left_n)
        right_pwm = norm_to_pwm(right_n)
    end

    apply_outputs(left_pwm, right_pwm, timeout_ms, slew_us)
    return update, 20
end

gcs:send_text(6, "tank_mixer.lua loaded")
return update, 20
