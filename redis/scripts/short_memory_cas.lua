local raw = redis.call('JSON.GET', KEYS[1])
local expected_version = tonumber(ARGV[1])
if raw then
  local current = cjson.decode(raw)
  if tonumber(current.version or 0) ~= expected_version then
    return 0
  end
elseif expected_version ~= 0 then
  return 0
end

redis.call('JSON.SET', KEYS[1], '$', ARGV[2])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
