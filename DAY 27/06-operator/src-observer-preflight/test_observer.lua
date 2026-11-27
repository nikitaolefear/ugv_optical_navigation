local observed, index = {}, 0
ahrs = {get_posvelyaw_source_set = function() return index end}
gcs = {send_text = function(_, severity, message)
    assert(severity == 6)
    observed[#observed + 1] = message
end}
function millis()
    return {toint = function() return 1234 end}
end
local tick, delay = assert(loadfile(arg[1]))()
assert(delay == 500)
for i = 0, 3 do
    index = i
    local again, interval = tick()
    assert(again == tick and interval == 500)
end
assert(#observed == 3)
for i = 1, 3 do
    assert(observed[i] == string.format("S3SRC1 %d 1234", i - 1))
end
print("Observer Lua mock PASS")
