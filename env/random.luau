local Random = {}
Random.__index = Random
local MODULUS = 2^31 - 1
local MULTIPLIER = 48271
local INCREMENT = 0
function Random.new(seed)
    local type = typeof(seed)
    assert(type == "number", `invalid argument #1 to 'new' (number expected, got {type})`)

    local self = setmetatable({}, Random)
    
    if seed == nil then
        seed = os.time() * 1000 + os.clock() * 1000000
        seed = math.floor(seed) % MODULUS
    end
    
    self._seed = math.floor(seed) % MODULUS
    if self._seed <= 0 then
        self._seed = 1
    end
    
    return self
end
function Random:_next()
    self._seed = (MULTIPLIER * self._seed + INCREMENT) % MODULUS
    return self._seed
end
function Random:NextInteger(min, max)
    if min == nil or max == nil then
        error("NextInteger requires min and max arguments")
    end
    
    min = math.floor(min)
    max = math.floor(max)
    
    if min > max then
        error("min must be less than or equal to max")
    end
    
    local range = max - min + 1
    local random = self:_next()
    
    return min + (random % range)
end
function Random:NextNumber(min, max)
    local random = self:_next() / MODULUS
    
    if min == nil and max == nil then
        return random
    elseif min ~= nil and max ~= nil then
        return min + random * (max - min)
    else
        error("NextNumber requires either no arguments or both min and max")
    end
end
function Random:NextUnitVector()
    local x, y, z, lengthSquared
    
    repeat
        x = self:NextNumber(-1, 1)
        y = self:NextNumber(-1, 1)
        z = self:NextNumber(-1, 1)
        lengthSquared = x*x + y*y + z*z
    until lengthSquared <= 1 and lengthSquared > 0
    
    local length = math.sqrt(lengthSquared)
    
    return {
        X = x / length,
        Y = y / length,
        Z = z / length
    }
end
function Random:Shuffle(tbl)
    if type(tbl) ~= "table" then
        error("Shuffle requires a table argument")
    end
    
    local n = #tbl
    
    for i = n, 2, -1 do
        local j = self:NextInteger(1, i)
        tbl[i], tbl[j] = tbl[j], tbl[i]
    end
    
    return tbl
end
function Random:Clone()
    local clone = Random.new(0)
    clone._seed = self._seed
    return clone
end
return Random