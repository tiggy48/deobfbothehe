--!nolint
--[[
    This module is for exporting exploit functions onto the environment, including things like:
    iscclosure, islclosure, Drawing library & other things.

    ONLY ADD EXPLOIT FUNCTIONS HERE, TO KEEP CODE CLEAN.
]]

local TypesLib = require("../mods/types")
local AstLib = require("../mods/ast")
local loadstring = require("@lune/luau").load
local task = require("@lune/task")
local base64 = require("../env/base64")

type AstExpression = TypesLib.AstExpression
type AstBlock = TypesLib.AstBlock

type func = (...any) -> ...any
type dict = { [any]: any }
type array = { any }
type tbl = dict | array

local exploits : dict = {
    env = {},
    funcs = {},
    libs = {}
}

local debuginfo = debug.info;
local pack = table.pack;
local insert = table.insert;

local started = os.clock()

local _typeof = typeof;
local fenv = getfenv();

local Ast = AstLib.Ast
local AST : AstBlock;
local Globals = {} :: { [string]: any }

local Waited = false;
local Variables : { [any] : string }
local ToAST : (...any) -> AstExpression;
local TupleToAST : (...any) -> (...AstExpression);
local AppendCall: (FuncName : string, ...any) -> (string, AstExpression)
local GetInternalValue : (obj : any) -> any
local Function: (Callback : func, FuncName : string, IsCClosure : boolean?) -> func
local SafeValue : (string, any, dict?) -> any
local GetMetatable : (string, dict?, dict?) -> any
local GetVar : () -> string
local Metatables: {
    [tbl] : {
        [string]: func
    }
} = {}

local IdentityLevel = 3

--> Locals:

local AllowedMethods = {
    GET = true,
    PUT = true,
    PATCH = true,
    DELETE = true,
    POST = true,
    OPTIONS = true,
    HEAD = true
}

--> helper funcs:

local function typeof(x : any) : ( string, any )
    local int = GetInternalValue(x)
    return exploits.env.types[x] or _typeof(int), int;
end

local function elapsedTime()
    return os.clock() - started
end

local function checktype(obj : any, expected : string, msg : string) : any
    local type, int = typeof(obj)
    if Variables[int] and type == "table" then --> FindFirstChild(unknown_value)
        return int;
    else
        assert(type == expected, `{msg}, {expected} expected got {type}`)
    end
    return int;
end

local function Define(Var : AstExpression | string, Value : any, IsGlobal : boolean, Raw : boolean?) : AstExpression
    exploits.env.OpCountCheck()

    local Type = typeof(Var)
    local ActualVar = Type == "string" and Ast.Variable(Var) or Var
    local Assign = Ast.Assign(
        {
            ActualVar
        },
        { if Raw then Value else ToAST(Value) },
        IsGlobal
    )

    Ast.addStatement(
        AST,
        Assign
    )

    if ActualVar.kind == "variable" then
        exploits.env.VarValues[ActualVar.data.name] = Value
    end

    return Assign;
end

local function DefineVar(...) : (string, AstExpression)
    local Var = GetVar()
    local Stat = Define(Var, ...)
    return Var, Stat
end

local function iscclosure(f : thread | (any) -> any) : boolean
    if exploits.env.cclosures[f] then return true end
    local Type = typeof(f)
    if Type ~= "function" then return false end

    local info = debuginfo(f, "s")
    return info == '[C]'
end

function exploits:setEnv(env)
    self.env = env;

    local needed = {
        "AST", "ToAST", "AppendCall", "SafeValue", "Spy", "TypeSpy", "cclosures", "types", "Function", "Settings", "GetInternalValue",
        "Settings", "UseHookOp", "TraceId", "Variables", "Globals", "GetMetatable", "GetVar", "TupleToAST", "OpCountCheck"
    }

    for _, v in needed do assert(env[v], "missing " .. v) end

    AST = env.AST;
    ToAST = env.ToAST;
    TupleToAST = env.TupleToAST
    AppendCall = env.AppendCall;
    Spy = env.Spy;
    TypeSpy = env.Spy;
    Function = env.Function
    GetInternalValue = env.GetInternalValue;
    Settings = env.Settings;
    UseHookOp = env.UseHookOp;
    TraceId = env.TraceId;
    Variables = env.Variables;
    Globals = env.Globals;
    SafeValue = env.SafeValue
    GetMetatable = env.GetMetatable
    GetVar = env.GetVar
end

function exploits:setGlobalEnv(env)
    Env = env;
end

function exploits:createFunction(func, name : string, ... : ( string ))
    local options = {...}
    options = options[#options]
    local cfg = {} :: { [string]: boolean }
    if _typeof(options) == "table" then
        cfg = options
    end

    for _, v in next, { name, ... } do
        self.funcs[v] = function(...)
            local var, Statement = AppendCall(v, ...)
            local values;
            -- wtf?
            if cfg.allyougot then
                values = pack(func(Statement, v, ...))
            else
                values = pack(func(v, ...))
            end
            local vars = { Ast.Variable(var) }
            local spied = { SafeValue(var, values[1]) }

            for i = 2, values.n do
                local v = GetVar()
                local val = values[i];

                insert(vars, Ast.Variable(v))
                insert(spied, if cfg.dont_return then val else SafeValue(v, val))
            end

            Statement.vars = vars
            return unpack(spied);
        end
    end
    return func;
end

exploits:createFunction(function(func : string, closure : func)
    checktype(closure, "function", "invalid argument #1 to '" .. func .. "'")
    return iscclosure(closure);
end, "iscclosure", "is_c_closure")

exploits:createFunction(function(funcName : string, closure : func)
    checktype(closure, "function", "invalid argument #1 to '" .. funcName .. "'")
    return not iscclosure(closure);
end, "islclosure", "is_l_closure")

exploits:createFunction(function(funcName : string, content : any)
    checktype(content, "string", `invalid argument #1 to '{funcName}'`)
end, "setclipboard", "setrbxclipboard")

exploits.funcs.spawn = function(callback : func)
    checktype(callback, "function", "invalid argument #1 to 'spawn'")

    local Beautified = ToAST(callback, {
        bypass_beautify = true,
        as_func = true,
        params = { math.random(1, 1e6) / 1e6, elapsedTime() }
    })
                
    AppendCall("spawn", Beautified)
end

exploits.funcs.delay = function(delayTime : number, callback : func)
    checktype(delayTime, "number", "invalid argument #1 to 'delay'")
    checktype(callback, "function", "invalid argument #2 to 'delay'")

    local Beautified = ToAST(callback, {
        bypass_beautify = true,
        as_func = true
    })

    AppendCall("delay", ToAST(delayTime), Beautified)
end

exploits.funcs.elapsedTime = function()
    return elapsedTime()
end

exploits.funcs.printidentity = function()
    Env.print(`Current identity is {IdentityLevel}`)
end

exploits:createFunction(function(funcName : string)
    return IdentityLevel
end, "getthreadidentity", "getidentity", "getthreadcontext")

exploits:createFunction(function(funcName : string, level : number)
    level = checktype(level, "number", `invalid argument #1 to '{funcName}'`)
    assert(level > 0 or level < 10, `invalid argument #1 to '{funcName}', level must be between 0 and 9!`)

    IdentityLevel = level
end, "sethreadidentity", "setidentity", "setthreadcontext")

exploits:createFunction(function(funcName : string)
    return "Krnl", "2.0.1";
end, "getexecutorname", "identifyexecutor")

exploits:createFunction(function(funcName : string, value : tbl)
    value = checktype(value, "table", `invalid argument #1 to '{funcName}'`)
    return table.isfrozen(value);
end, "isfrozen")

exploits:createFunction(function(funcName : string, time : number)
    for _, v in next, Globals.Deferred do
        v()
    end

    assert(coroutine.isyieldable(), "attempt to yield across metamethod/C-call boundary") -- i meow alot

    time = time or 0.03

    Globals.Delay += time
    return time + math.random(), os.time() - Globals.StartedRunning
end, "wait")

exploits:createFunction(function(stat : AstExpression, funcName : string, ... : any)
    local stringed = ""

    for _, thing in next, stat.data.values do
        stringed ..= AstLib.ExprToCode(thing) .. " "
    end
    insert(Globals.Logs, {
        message = stringed:sub(1, -2),
        timestamp = os.clock() + Globals.Delay, --> nice
        --messageType = Enum.MessageInfo.axb --> not nice
    })
    return nil;
end, "print", "warn", {
    allyougot = true
})

exploits:createFunction(function(funcName : string, obj : any)
    if typeof(obj) == "Instance" then
        --> return the faked mt..
        local Spoofed = Metatables[obj]
        if Spoofed then return Spoofed end --> Prevent duplicates.

        local metatable = getmetatable(obj) --> the actual metatable used in logging
        Metatables[obj] = metatable

        return metatable;
    end
    return nil;
end, "getrawmetatable")

exploits:createFunction(function(stat : AstExpression, funcName : string, obj : any, mt : dict)
    checktype(mt, "table", "invalid argument #1 to '" .. funcName .. "'")

    if typeof(obj) == "Instance" then
        --> return the faked mt..
        local metatable = getmetatable(obj);
        for i, v in next, mt do
            local old = metatable[i]
            metatable[i] = function(...)
                old(...)

                local values = pack(v(...))
                local spied = {}

                for i = 1, values.n do
                    local var = GetVar()

                    stat.data.vars[i] = Ast.Variable(var)
                    insert(spied, SafeValue(var, values[i]))
                end
                return unpack(spied)
            end
        end
        Metatables[obj] = metatable;
        return metatable;
    end
    return mt;
end, "setrawmetatable", {
    allyougot = true
})

exploits:createFunction(function(funcName : string)
    return Globals.HWID;
end, "gethwid", "get_hwid")

exploits:createFunction(function(stat : AstExpression, funcName : string, data : { Url : string, [string]: any })
    checktype(data, "table", `invalid argument #1 to '{funcName}'`)

    print("reueqst")

    assert(data.Url or data.url, "missing 'Url' field!")

    local Method = data.Method or data.method or "GET"

    assert(AllowedMethods[Method], "method not allowed!")

    local Var = DefineVar(
        Ast.Index(
            stat.data.vars[1],
            Ast.String("Body")
        )
    )

    return {
        StatusCode = 200,
        StatusMessage = "OK",
        Body = TypeSpy(Var) --> fix?
    }
end, "request", "http_request", {
    allyougot = true,
    dont_return = true
})

exploits:createFunction(function(stat : AstExpression, funcName : string, closure : func)
    checktype(closure, "function", "invalid argument #1 to '" .. funcName .. "'")

    return Function(function(...) return closure(...) end, stat.data.vars[1].data.name, iscclosure(closure))
end, "clonefunction", {
    allyougot = true
})

exploits:createFunction(function(funcName : string)
    return true;
end, "checkcaller")

exploits:createFunction(function(funcName : string, param : string)
    checktype(param, "string", `invalid argument #1 to '{funcName}'`)
end, "queue_on_teleport", "queueonteleport")

exploits.libs.task = {
    wait = function(time : number)
        AppendCall("task.wait", time)

        if time then
            checktype(time, "number", "invalid argument #1 to 'wait'")
        end

        for _, v in next, Globals.Deferred do
            v()
        end

        time = GetInternalValue(time);

        assert(coroutine.isyieldable(), "thread is not yieldable")

        if _typeof(time) == "number" then
            time = time or Globals.FRAME_TIME

            Globals.Delay += time
            if not Waited and time <= 1 then Waited = true return task.wait(time) end
            return time + (math.random(1e5, 1e6) / 1e7)--time + math.random(), os.time() - Globals.StartedRunning
        end
        return (math.random(1e5, 1e6) / 1e7)
    end,
    delay = function(time : number, callback : func)
        checktype(time, "number", "invalid argument #1 to 'delay'")
        checktype(callback, "function", "invalid argument #2 to 'delay'")

        AppendCall(
            "task.delay",
            time,
            ToAST(
                callback,
                {
                    delay = Globals.Delay + time,
                    --bypass_beautify = true
                }
            )
        )
        if time <= 1 then task.delay(time, fenv.setfenv(callback, Env)); end
    end,
    spawn = function(callback : func, ...)
        checktype(callback, "function", "invalid argument #1 to 'spawn'")
                
        local Var = AppendCall(
            "task.spawn",
            ToAST(
                callback,
                {
                    params = { ... },
                    delay = 0,
                    --bypass_beautify = true
                }
            ),
            ...
        )

        local Spied = Spy(Var)
        exploits.env.types[Spied] = "thread"
        return Spied;
    end,
    cancel = function(thread : thread)
        checktype(thread, "thread", "invalid argument #1 to 'cancel'")

        AppendCall("task.cancel", thread)
    end,
    defer = function(callback : func)
        checktype(callback, "function", "invalid argument #1 to 'defer'")
                
        insert(Globals.Deferred, callback)
        AppendCall("task.defer", ToAST(callback, {
            deferred = true
        }))
    end,
}

--/ base64

exploits.libs.base64 = {
    encode = function(data : string)
        local Var = AppendCall("base64.encode", data)
        checktype(data, "string", `invalid argument #1 to 'encode'`)
        return SafeValue(Var, base64.encode(data))
    end,
    decode = function(data : string)
        local Var = AppendCall("base64.decode", data)
        checktype(data, "string", `invalid argument #1 to 'decode'`)
        return SafeValue(Var, base64.decode(data))
    end
}

exploits:createFunction(function(funcName : string, data : string)
    checktype(data, "string", `invalid argument #1 to '{funcName}'`)
    return base64.encode(data)
end, "base64encode", "base64_encode", "b64encode")

exploits:createFunction(function(funcName : string, data : string)
    checktype(data, "string", `invalid argument #1 to '{funcName}'`)
    return base64.decode(data)
end, "base64decode", "base64_decode", "b64decode")

exploits:createFunction(function(funcName : string, obj : any, property : string)
    checktype(obj, "Instance", `invalid argument #1 to '{funcName}'`)
    checktype(property, "string", `invalid argument #2 to '{funcName}'`)

    local Callbacks = Globals.Callbacks[obj]
    if not Callbacks then return end

    for _, Callback in next, Callbacks do
        if Callback.Key == property then
            return Callback.Callback;
        end
    end
end, "getcallbackvalue")

exploits.funcs.rawset = function(tbl, k, v)
    tbl = checktype(tbl, "table", "invalid argument #1 to 'rawset'")

    return rawset(tbl, k, v)
end

exploits.funcs.loadstring = function(src)
    local intSrc = GetInternalValue(src)
    local Type = typeof(intSrc)
    local UseMe = _typeof(intSrc)
    local _src = src;

    if Type == "string" then
        src = intSrc;

        local tp = _typeof(src)

        if tp == "string" then
            local DontHook = not Settings.hookOp;
            if src:gsub("\n","") == 'return pcall(function()return 1/"abc"end)' then
                if not Globals.Pristine then
                    Globals.Pristine = true
                    Settings.roblox = true
                end

                AppendCall("loadstring", src)

                return setfenv(loadstring(src, {
                    debugName = TraceId
                }), Env)
            end

            if not DontHook then
                local Success, Hooked = UseHookOp(src)
                if Success then src = Hooked end
            end

            if #src > 100 and Settings.from_ld then
                Ast.addStatement(
                    AST,
                    Ast.FunctionCall(
                        Ast.Variable("pcall"),
                        {
                            Ast.Variable("loadstring"),
                            ToAST(_src)
                        }
                    )
                )
            end

            local Start = src:sub(1, 8)
            if Start == "return f" or Start == "return(f" or Start == "return({" or #src > 10_000 then -- Ignore moonsec / obfuscation garbage
                local Value = loadstring(src)
                if not Value then return nil, "Cannot require a non-RobloxScript module from a RobloxScript" end

                return setfenv(Value, Env);
            end
        else
            Type = tp
        end
    end

    if
        (Type == "table" and not Variables[src]) or Type == "number"-- or (Type == "string" and src:sub(1, 9) == "function:")
    then
        error(`invalid argument #1 to 'loadstring', string expected got '{Type}'`)
    end

    local Returned = AppendCall("loadstring", _src)

    if UseMe ~= "string" then
        -- unavailable function?
        if UseMe == "table" then
            return Function(function(...)
                return Spy(AppendCall(Returned, ...))
            end, Returned, true)
        else
            error(`invalid argument #1 to 'loadstring', string expected got '{Type}'`)
        end
    end

    local Success, Value = pcall(loadstring, src)
    if not Success or not Value then
        return nil, tostring(Value):match("[^\n]+")
    end

    return Function(function(...)
        local MeowVar = GetVar()
        local Statement = Define(
            MeowVar,
            Ast.FunctionCall(Ast.Variable(Returned), TupleToAST(...)),
            false,
            true
        )

        local Results = pack(pcall(fenv.setfenv(Value, Env), ...)) -- using fenv. to ignore the stupid error

        local Vars = {}
        local Spied = {}

        if not Results[1] then
            return Spy(Statement.data.vars[1].data.name)
        end

        for i = 2, Results.n do
            local Var = GetVar()

            insert(Vars, Ast.Variable(Var))
            insert(Spied, SafeValue(Var, Results[i]))
        end

        Statement.data.vars = Vars

        print(Spied, Results)

        return unpack(Spied);
    end, Returned, false)
end

return exploits