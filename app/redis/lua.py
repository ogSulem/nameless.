from __future__ import annotations

MATCH_RESERVE_LUA = r"""
-- KEYS are unused (script uses ARGV only)
-- ARGV:
-- 1: me_tg_id
-- 2: lock_ttl_ms
-- 3..N: queue names

local me = ARGV[1]
local ttl = tonumber(ARGV[2])

local function lock_key(uid)
  return "lock:match:" .. uid
end

local function active_key(uid)
  return "active_dialog:" .. uid
end

if redis.call("GET", active_key(me)) then
  return {"ACTIVE"}
end

for i = 3, #ARGV do
  local q = ARGV[i]

  local candidate = redis.call("RPOP", q)
  if candidate then
    if candidate == me then
      redis.call("LPUSH", q, candidate)
    elseif redis.call("GET", active_key(candidate)) then
      redis.call("LPUSH", q, candidate)
    else
      if redis.call("SET", lock_key(candidate), "1", "NX", "PX", ttl) then
        return {"OK", candidate, q}
      else
        redis.call("LPUSH", q, candidate)
      end
    end
  end
end

return {"NONE"}
"""
