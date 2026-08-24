local DateTime = {}
DateTime.__index = DateTime

-- Constants
local DAYS = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31}
local DAYS_LEAP = {31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31}

-- Helpers
local function IsLeap(y)
	return y % 4 == 0 and (y % 100 ~= 0 or y % 400 == 0)
end

local function UnixToDate(ts)
	local d = math.floor(ts / 86400)
	local s = ts % 86400
	local h = math.floor(s / 3600)
	local m = math.floor(s % 3600 / 60)
	local sec = s % 60
	
	local y = 1970
	while true do
		local dy = IsLeap(y) and 366 or 365
		if d < dy then break end
		d -= dy
		y += 1
	end
	
	local mon = 1
	local ml = IsLeap(y) and DAYS_LEAP or DAYS
	while d >= ml[mon] do
		d -= ml[mon]
		mon += 1
	end
	
	return y, mon, d + 1, h, m, sec
end

local function DateToUnix(y, mon, d, h, m, s)
	local days = 0
	for yr = 1970, y - 1 do
		days += IsLeap(yr) and 366 or 365
	end
	
	local ml = IsLeap(y) and DAYS_LEAP or DAYS
	for i = 1, mon - 1 do
		days += ml[i]
	end
	
	days += d - 1
	return days * 86400 + h * 3600 + m * 60 + s
end

-- Constructors
function DateTime.Now()
	return setmetatable({_ts = os.time()}, DateTime)
end

DateTime.now = DateTime.Now

function DateTime.FromUnixTimestamp(ts)
	return setmetatable({_ts = ts}, DateTime)
end

function DateTime.FromUnixTimestampMillis(ms)
	return setmetatable({_ts = ms / 1000}, DateTime)
end

DateTime.fromUnixTimestampMillis = DateTime.FromUnixTimestampMillis

function DateTime.FromUniversalTime(y, mon, d, h, m, s, ms)
	local ts = DateToUnix(y or 1970, mon or 1, d or 1, h or 0, m or 0, s or 0)
	return setmetatable({_ts = ts + (ms or 0) / 1000}, DateTime)
end

DateTime.fromUniversalTime = DateTime.FromUniversalTime

function DateTime.FromLocalTime(y, mon, d, h, m, s, ms)
	return DateTime.FromUniversalTime(y, mon, d, h, m, s, ms)
end

DateTime.fromLocalTime = DateTime.FromLocalTime

function DateTime.FromIsoDate(str)
	local y, mon, d, h, m, s, ms = str:match("(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)%.?(%d*)")
	if not y then error("Invalid ISO date") end
	return DateTime.FromUniversalTime(
		tonumber(y), tonumber(mon), tonumber(d),
		tonumber(h), tonumber(m), tonumber(s),
		ms ~= "" and tonumber(ms) or 0
	)
end

DateTime.fromIsoDate = DateTime.FromIsoDate

-- Methods
function DateTime:ToUniversalTime()
	local y, mon, d, h, m, s = UnixToDate(math.floor(self._ts))
	return {
		Year = y, Month = mon, Day = d,
		Hour = h, Minute = m, Second = s,
		Millisecond = math.floor((self._ts % 1) * 1000)
	}
end

function DateTime:ToLocalTime()
	return self:ToUniversalTime()
end

function DateTime:FormatUniversalTime(fmt)
	local t = self:ToUniversalTime()
	return (fmt or "L LT")
		:gsub("YYYY", string.format("%04d", t.Year))
		:gsub("MM", string.format("%02d", t.Month))
		:gsub("DD", string.format("%02d", t.Day))
		:gsub("HH", string.format("%02d", t.Hour))
		:gsub("mm", string.format("%02d", t.Minute))
		:gsub("ss", string.format("%02d", t.Second))
		:gsub("L", string.format("%02d/%02d/%04d", t.Month, t.Day, t.Year))
		:gsub("LT", string.format("%02d:%02d", t.Hour, t.Minute))
end

function DateTime:FormatLocalTime(fmt)
	return self:FormatUniversalTime(fmt)
end

function DateTime:ToIsoDate()
	local t = self:ToUniversalTime()
	return string.format("%04d-%02d-%02dT%02d:%02d:%02d.%03dZ",
		t.Year, t.Month, t.Day, t.Hour, t.Minute, t.Second, t.Millisecond)
end

function DateTime:__tostring()
	return self:ToIsoDate()
end

-- Properties
function DateTime:GetUnixTimestamp()
	return self._ts
end

function DateTime:GetUnixTimestampMillis()
	return self._ts * 1000
end

-- Operators
function DateTime:__sub(other)
	return getmetatable(other) == DateTime 
		and self._ts - other._ts 
		or DateTime.FromUnixTimestamp(self._ts - other)
end

function DateTime:__add(sec)
	return DateTime.FromUnixTimestamp(self._ts + sec)
end

function DateTime:__eq(other)
	return self._ts == other._ts
end

function DateTime:__lt(other)
	return self._ts < other._ts
end

function DateTime:__le(other)
	return self._ts <= other._ts
end

return DateTime