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
redis.call('HSET', KEYS[2],
  'reservation_id', ARGV[9],
  'token_hash', ARGV[10],
  'generation', generation,
  'expires_at', ARGV[11])
redis.call('EXPIRE', KEYS[2], ARGV[12])
redis.call('ZADD', KEYS[3], ARGV[11], ARGV[9])
return 'PROJECTED'
