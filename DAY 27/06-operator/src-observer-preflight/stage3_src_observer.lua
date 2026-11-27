-- Read-only Stage 3 observer. Install only under a separately approved bench procedure.
local function update()
    local source = ahrs:get_posvelyaw_source_set()
    if source >= 0 and source <= 2 then
        gcs:send_text(6, string.format("S3SRC1 %u %u", source, millis():toint()))
    end
    return update, 500
end

return update, 500
