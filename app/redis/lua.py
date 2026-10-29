from __future__ import annotations

# Atomically pick and reserve one candidate from one or more ZSET queues.
#
# ARGV[1] = me (telegram id)
# ARGV[2] = now (epoch seconds, passed by the client: TIME is not allowed in
#            a script that also performs writes)
# ARGV[3] = lock ttl in milliseconds
# ARGV[4] = max candidates to probe
# ARGV[5..] = queue keys (in priority order)
#
# Returns {candidate_tg, queue_key, score} or {} when nothing is available.
RESERVE_LUA = r"""
local me = ARGV[1]
local now = tonumber(ARGV[2]) or 0
local ttl = tonumber(ARGV[3]) or 4000
local probe = tonumber(ARGV[4]) or 25

for i = 5, #ARGV do
  local q = ARGV[i]
  local items = redis.call("ZRANGEBYSCORE", q, "-inf", now, "LIMIT", 0, probe)
  for _, cand in ipairs(items) do
    if cand == me then
      -- never match with self, but keep the entry in the queue
    elseif redis.call("EXISTS", "lock:match:" .. cand) == 1 then
      -- someone else is reserving this candidate right now
    elseif redis.call("EXISTS", "active_dialog:" .. cand) == 1 then
      redis.call("ZREM", q, cand)
    else
      local score = redis.call("ZSCORE", q, cand)
      redis.call("ZREM", q, cand)
      redis.call("SET", "lock:match:" .. cand, me, "PX", ttl)
      return {cand, q, score or tostring(now)}
    end
  end
end

return {}
"""

# Transactional claim of a reserved candidate: only succeeds for the reserving user.
# ARGV[1]=me ARGV[2]=candidate ARGV[3]=ttl_ms
CLAIM_LUA = r"""
if redis.call("GET", "lock:match:" .. ARGV[2]) == ARGV[1] then
  redis.call("SET", "lock:match:" .. ARGV[2], ARGV[1], "PX", ARGV[3])
  return 1
end
return 0
"""

# Release my reservation, but only if I still own it.
RELEASE_LUA = r"""
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
end
return 0
"""
