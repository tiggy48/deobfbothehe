local Color3 = require("@lune/roblox").Color3

local TERRAIN_MATERIAL_COLORS = {
    ["Grass"] = Color3.new(0.35, 0.5, 0.25),
    ["Slate"] = Color3.new(0.35, 0.35, 0.4),
    ["Concrete"] = Color3.new(0.5, 0.5, 0.5),
    ["Brick"] = Color3.new(0.65, 0.35, 0.3),
    ["Sand"] = Color3.new(0.8, 0.75, 0.6),
    ["WoodPlanks"] = Color3.new(0.6, 0.45, 0.3),
    ["Rock"] = Color3.new(0.5, 0.5, 0.5),
    ["Glacier"] = Color3.new(0.75, 0.85, 0.9),
    ["Snow"] = Color3.new(0.9, 0.9, 0.9),
    ["Sandstone"] = Color3.new(0.7, 0.65, 0.55),
    ["Mud"] = Color3.new(0.45, 0.35, 0.25),
    ["Basalt"] = Color3.new(0.2, 0.2, 0.2),
    ["Ground"] = Color3.new(0.35, 0.45, 0.3),
    ["CrackedLava"] = Color3.new(0.35, 0.35, 0.35),
    ["Asphalt"] = Color3.new(0.3, 0.3, 0.3),
    ["Cobblestone"] = Color3.new(0.5, 0.5, 0.5),
    ["Salt"] = Color3.new(0.85, 0.85, 0.85),
    ["Limestone"] = Color3.new(0.85, 0.85, 0.8),
    ["Pavement"] = Color3.new(0.5, 0.5, 0.55),
    ["LeafyGrass"] = Color3.new(0.3, 0.5, 0.3),
}

local INVALID_TERRAIN_MATERIALS = {
    ["Metal"] = true,
    ["DiamondPlate"] = true,
    ["Plastic"] = true,
    ["SmoothPlastic"] = true,
    ["Neon"] = true,
    ["Wood"] = true,
    ["Fabric"] = true,
    ["Marble"] = true,
    ["Granite"] = true,
    ["Glass"] = true,
    ["ForceField"] = true,
    ["Ice"] = true,
    ["CorrodedMetal"] = true,
    ["Foil"] = true,
    ["Pebble"] = true,
}

return function(material)
    if INVALID_TERRAIN_MATERIALS[material] then
        error("Invalid terrain material")
    end
    
    local color = TERRAIN_MATERIAL_COLORS[material]
    if color then
        return color
    end

    error("Unsupported terrain material")
end