local current_generation = tonumber(redis.call('HGET', KEYS[1], 'generation') or '-1')
local generation = tonumber(ARGV[1])
if current_generation > generation then
  return 'STALE_GENERATION'
end
local current_version = tonumber(redis.call('HGET', KEYS[1], 'version') or '-1')
local version = tonumber(ARGV[2])
if current_generation == generation and current_version > version then
  return 'STALE_VERSION'
end
redis.call('HSET', KEYS[1],
  'generation', generation,
  'version', version,
  'total', ARGV[3],
  'available', ARGV[4],
  'held', ARGV[5],
  'confirmed', ARGV[6],
  'status', ARGV[7],
  'updated_at', ARGV[8])
local hold_generation = tonumber(redis.call('HGET', KEYS[2], 'generation') or '-1')
if hold_generation <= generation then
  redis.call('DEL', KEYS[2])
  redis.call('ZREM', KEYS[3], ARGV[9])
end
return 'PROJECTED'
