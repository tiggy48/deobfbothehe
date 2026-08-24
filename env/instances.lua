local function getInstances(apiDump)
    local instances, classMap = {}, {}
    
    for _, class in pairs(apiDump.Classes) do
        classMap[class.Name] = class
    end
    
    local function collectMembers(className, visited)
        visited = visited or {}
        if visited[className] or not classMap[className] then return {}, {}, {}, {} end
        visited[className] = true
        
        local methods, properties, events, callbacks = {}, {}, {}, {}
        
        if classMap[className].Superclass then
            local pm, pp, pe, pc = collectMembers(classMap[className].Superclass, visited)
            for name, member in pairs(pm) do methods[name] = member end
            for name, member in pairs(pp) do properties[name] = member end
            for name, member in pairs(pe) do events[name] = member end
            for name, member in pairs(pc) do callbacks[name] = member end
        end
        
        if classMap[className].Members then
            for _, member in pairs(classMap[className].Members) do
                if member.MemberType == "Function" then
                    methods[member.Name] = {Name = member.Name, Parameters = member.Parameters or {}, ReturnType = member.ReturnType}
                elseif member.MemberType == "Property" then
                    properties[member.Name] = {Name = member.Name, ValueType = member.ValueType, CanLoad = not (member.Tags and table.find(member.Tags, "NotScriptable")), CanSave = not (member.Tags and table.find(member.Tags, "ReadOnly"))}
                elseif member.MemberType == "Event" then
                    events[member.Name] = {Name = member.Name, Parameters = member.Parameters or {}}
                elseif member.MemberType == "Callback" then
                    callbacks[member.Name] = {Name = member.Name, Parameters = member.Parameters or {}, ReturnType = member.ReturnType}
                end
            end
        end
        
        return methods, properties, events, callbacks
    end
    
    for _, class in pairs(apiDump.Classes) do
        --[[if class.Tags then
            for _, tag in pairs(class.Tags) do
                if tag == "NotCreatable" or tag == "Service" then continue end
            end
        end]]
        
        local methods, properties, events, callbacks = collectMembers(class.Name)
        instances[class.Name] = {methods = methods, properties = properties, events = events, callbacks = callbacks, tags = class.Tags}
    end
    
    return instances
end

local apiDump = require("@lune/serde").decode("json", require("@lune/fs").readFile("./env/api.json"))

local hierarchy = {}
for _, class in pairs(apiDump.Classes) do
    hierarchy[class.Name] = class.Superclass
end

local function IsA(self, class2)
    local current = self.ClassName;
    while current do
        if current == class2 then return true end
        current = hierarchy[current]
    end
    return false
end

return {
    getInstances(apiDump),
    {
        IsA = IsA,
        isA = IsA
    }
}