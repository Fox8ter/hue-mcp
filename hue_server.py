"""
Philips Hue Controller MCP Server — HTTP Streamable + Legacy SSE

- Endpoint HTTP streamable:
  POST /mcp  (JSON unique ou SSE si Accept: text/event-stream)
  GET  /mcp  (SSE push/keepalive optionnel)

- Legacy SSE MCP conservé sous:  /legacy

Env:
  HUE_BRIDGE_IP / BRIDGE_IP      -> IP du bridge Hue (sinon discovery)
  MCP_HOST (default 0.0.0.0)
  MCP_PORT (default 8080)
  ALLOWED_ORIGINS="https://n8n.example.com,https://autre.origine"
"""

import json
import os
import logging
import uuid
import inspect
import asyncio
from typing import Any, Dict, List, Optional, Tuple, Union, get_type_hints, get_origin, get_args
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass

from phue import Bridge
from mcp.server.fastmcp import FastMCP, Context

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

# --- Configuration ---

# Bridge IP depuis l'environnement (peut rester None -> discovery)
bridge_ip = os.getenv("BRIDGE_IP") or os.getenv("HUE_BRIDGE_IP")

CONFIG_DIR = os.path.expanduser("~/.hue-mcp")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("hue-mcp")

# --- Server Context Setup ---

@dataclass
class HueContext:
    """Context object holding the Hue bridge connection."""
    bridge: Bridge
    light_info: Dict  # Cache of light information

@asynccontextmanager
async def hue_lifespan(server: FastMCP) -> AsyncIterator[HueContext]:
    """
    Manage connection to Hue Bridge:
    - Discovery/connection
    - Save/load config
    - Build light cache
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)

    _bridge_ip = os.getenv("BRIDGE_IP") or os.getenv("HUE_BRIDGE_IP")
    logger.info(f"Using bridge_ip={_bridge_ip}")
    bridge_username = None

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                _bridge_ip = _bridge_ip or config.get("bridge_ip")
                bridge_username = config.get("username")
                logger.info(f"Loaded configuration from {CONFIG_FILE} (bridge_ip={_bridge_ip})")
        except Exception as e:
            logger.error(f"Error loading config: {e}")

    try:
        if not _bridge_ip:
            logger.info("No bridge IP specified, attempting discovery...")
            bridge = Bridge()  # discovery
            _bridge_ip = bridge.ip
            logger.info(f"Discovered bridge at {_bridge_ip}")
        else:
            logger.info(f"Connecting to bridge at {_bridge_ip}")
            bridge = Bridge(_bridge_ip, username=bridge_username)

        bridge.connect()

        with open(CONFIG_FILE, "w") as f:
            json.dump({"bridge_ip": bridge.ip, "username": bridge.username}, f)
            logger.info(f"Saved configuration to {CONFIG_FILE}")

        light_info = bridge.get_light()
        yield HueContext(bridge=bridge, light_info=light_info)

    except Exception as e:
        logger.error(f"Error connecting to Hue bridge: {e}")
        raise
    finally:
        pass

# Create MCP server
mcp = FastMCP("Philips Hue Controller", lifespan=hue_lifespan, dependencies=["phue"])

# --- Utility Functions ---

def get_bridge_ctx(ctx: Context) -> Tuple[Bridge, Dict]:
    hue_ctx = ctx.request_context.lifespan_context
    return hue_ctx.bridge, hue_ctx.light_info

def rgb_to_xy(r: int, g: int, b: int) -> List[float]:
    r, g, b = r/255.0, g/255.0, b/255.0
    r = pow(r, 2.2) if r > 0.04045 else r/12.92
    g = pow(g, 2.2) if g > 0.04045 else g/12.92
    b = pow(b, 2.2) if b > 0.04045 else b/12.92
    X = r * 0.649926 + g * 0.103455 + b * 0.197109
    Y = r * 0.234327 + g * 0.743075 + b * 0.022598
    Z = r * 0.000000 + g * 0.053077 + b * 1.035763
    sum_XYZ = X + Y + Z
    if sum_XYZ == 0:
        return [0, 0]
    x = X / sum_XYZ
    y = Y / sum_XYZ
    return [x, y]

def validate_light_id(light_id: int, light_info: Dict) -> bool:
    return str(light_id) in light_info

def validate_group_id(group_id: int, bridge: Bridge) -> bool:
    groups = bridge.get_group()
    return str(group_id) in groups

def format_light_info(light_info: Dict) -> Dict:
    result = {}
    for light_id, light in light_info.items():
        result[light_id] = {
            "name": light["name"],
            "on": light["state"]["on"],
            "reachable": light["state"].get("reachable", True),
            "brightness": light["state"].get("bri"),
            "color_mode": light["state"].get("colormode"),
            "type": light["type"],
            "model": light.get("modelid"),
            "manufacturer": light.get("manufacturername"),
        }
    return result

# --- Tools & Resources ---

@mcp.tool()
def get_all_lights(ctx: Context) -> str:
    bridge, light_info = get_bridge_ctx(ctx)
    formatted_info = format_light_info(light_info)
    return json.dumps(formatted_info, indent=2)

@mcp.tool()
def get_light(light_id: int, ctx: Context) -> str:
    bridge, light_info = get_bridge_ctx(ctx)
    try:
        light_id_str = str(light_id)
        if light_id_str not in light_info:
            return f"Error: Light with ID {light_id} not found."
        return json.dumps(light_info[light_id_str], indent=2)
    except Exception as e:
        logger.error(f"Error getting light {light_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def get_all_groups(ctx: Context) -> str:
    bridge, _ = get_bridge_ctx(ctx)
    try:
        groups = bridge.get_group()
        formatted_groups = {}
        for group_id, group in groups.items():
            formatted_groups[group_id] = {
                "name": group["name"],
                "type": group["type"],
                "lights": group["lights"],
                "on": group["state"]["all_on"],
                "any_on": group["state"]["any_on"],
            }
        return json.dumps(formatted_groups, indent=2)
    except Exception as e:
        logger.error(f"Error getting groups: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def get_group(group_id: int, ctx: Context) -> str:
    bridge, _ = get_bridge_ctx(ctx)
    try:
        groups = bridge.get_group()
        gid = str(group_id)
        if gid not in groups:
            return f"Error: Group with ID {group_id} not found."
        return json.dumps(groups[gid], indent=2)
    except Exception as e:
        logger.error(f"Error getting group {group_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def get_all_scenes(ctx: Context) -> str:
    bridge, _ = get_bridge_ctx(ctx)
    try:
        scenes = bridge.get_scene()
        formatted_scenes = {}
        for scene_id, scene in scenes.items():
            formatted_scenes[scene_id] = {
                "name": scene.get("name", "Unknown"),
                "type": scene.get("type", "Unknown"),
                "group": scene.get("group"),
                "lights": scene.get("lights", []),
                "owner": scene.get("owner"),
            }
        return json.dumps(formatted_scenes, indent=2)
    except Exception as e:
        logger.error(f"Error getting scenes: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def turn_on_light(light_id: int, ctx: Context) -> str:
    bridge, light_info = get_bridge_ctx(ctx)
    try:
        if not validate_light_id(light_id, light_info):
            return f"Error: Light with ID {light_id} not found."
        bridge.set_light(light_id, "on", True)
        return f"Light {light_id} ({light_info[str(light_id)]['name']}) turned on."
    except Exception as e:
        logger.error(f"Error turning on light {light_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def turn_off_light(light_id: int, ctx: Context) -> str:
    bridge, light_info = get_bridge_ctx(ctx)
    try:
        if not validate_light_id(light_id, light_info):
            return f"Error: Light with ID {light_id} not found."
        bridge.set_light(light_id, "on", False)
        return f"Light {light_id} ({light_info[str(light_id)]['name']}) turned off."
    except Exception as e:
        logger.error(f"Error turning off light {light_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def set_brightness(light_id: int, brightness: int, ctx: Context) -> str:
    if not 0 <= brightness <= 254:
        return "Error: Brightness must be between 0 and 254."
    bridge, light_info = get_bridge_ctx(ctx)
    try:
        if not validate_light_id(light_id, light_info):
            return f"Error: Light with ID {light_id} not found."
        if not light_info[str(light_id)]["state"]["on"]:
            bridge.set_light(light_id, "on", True)
        bridge.set_light(light_id, "bri", brightness)
        percentage = round((brightness / 254) * 100)
        return f"Light {light_id} ({light_info[str(light_id)]['name']}) brightness set to {brightness} ({percentage}%)."
    except Exception as e:
        logger.error(f"Error setting brightness for light {light_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def set_color_rgb(light_id: int, red: int, green: int, blue: int, ctx: Context) -> str:
    if not all(0 <= c <= 255 for c in (red, green, blue)):
        return "Error: RGB values must be between 0 and 255."
    bridge, light_info = get_bridge_ctx(ctx)
    try:
        if not validate_light_id(light_id, light_info):
            return f"Error: Light with ID {light_id} not found."
        if "xy" not in light_info[str(light_id)]["state"]:
            return f"Error: Light {light_id} ({light_info[str(light_id)]['name']}) does not support color."
        if not light_info[str(light_id)]["state"]["on"]:
            bridge.set_light(light_id, "on", True)
        xy = rgb_to_xy(red, green, blue)
        bridge.set_light(light_id, "xy", xy)
        return f"Light {light_id} ({light_info[str(light_id)]['name']}) color set to RGB({red}, {green}, {blue})."
    except Exception as e:
        logger.error(f"Error setting RGB color for light {light_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def turn_on_group(group_id: int, ctx: Context) -> str:
    bridge, _ = get_bridge_ctx(ctx)
    try:
        if not validate_group_id(group_id, bridge):
            return f"Error: Group with ID {group_id} not found."
        group_info = bridge.get_group(group_id)
        group_name = group_info.get("name", f"Group {group_id}")
        bridge.set_group(group_id, "on", True)
        return f"Group {group_id} ({group_name}) turned on."
    except Exception as e:
        logger.error(f"Error turning on group {group_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def turn_off_group(group_id: int, ctx: Context) -> str:
    bridge, _ = get_bridge_ctx(ctx)
    try:
        if not validate_group_id(group_id, bridge):
            return f"Error: Group with ID {group_id} not found."
        group_info = bridge.get_group(group_id)
        group_name = group_info.get("name", f"Group {group_id}")
        bridge.set_group(group_id, "on", False)
        return f"Group {group_id} ({group_name}) turned off."
    except Exception as e:
        logger.error(f"Error turning off group {group_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def set_group_brightness(group_id: int, brightness: int, ctx: Context) -> str:
    if not 0 <= brightness <= 254:
        return "Error: Brightness must be between 0 and 254."
    bridge, _ = get_bridge_ctx(ctx)
    try:
        if not validate_group_id(group_id, bridge):
            return f"Error: Group with ID {group_id} not found."
        group_info = bridge.get_group(group_id)
        group_name = group_info.get("name", f"Group {group_id}")
        if not group_info["state"]["any_on"]:
            bridge.set_group(group_id, "on", True)
        bridge.set_group(group_id, "bri", brightness)
        percentage = round((brightness / 254) * 100)
        return f"Group {group_id} ({group_name}) brightness set to {brightness} ({percentage}%)."
    except Exception as e:
        logger.error(f"Error setting brightness for group {group_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def set_group_color_rgb(group_id: int, red: int, green: int, blue: int, ctx: Context) -> str:
    if not all(0 <= c <= 255 for c in (red, green, blue)):
        return "Error: RGB values must be between 0 and 255."
    bridge, _ = get_bridge_ctx(ctx)
    try:
        if not validate_group_id(group_id, bridge):
            return f"Error: Group with ID {group_id} not found."
        group_info = bridge.get_group(group_id)
        group_name = group_info.get("name", f"Group {group_id}")
        if not group_info["state"]["any_on"]:
            bridge.set_group(group_id, "on", True)
        xy = rgb_to_xy(red, green, blue)
        bridge.set_group(group_id, "xy", xy)
        return f"Group {group_id} ({group_name}) color set to RGB({red}, {green}, {blue})."
    except Exception as e:
        logger.error(f"Error setting color for group {group_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def set_scene(group_id: int, scene_id: str, ctx: Context) -> str:
    bridge, _ = get_bridge_ctx(ctx)
    try:
        if not validate_group_id(group_id, bridge):
            return f"Error: Group with ID {group_id} not found."
        scenes = bridge.get_scene()
        if scene_id not in scenes:
            return f"Error: Scene with ID {scene_id} not found."
        group_name = bridge.get_group(group_id).get("name", f"Group {group_id}")
        scene_name = scenes[scene_id].get("name", f"Scene {scene_id}")
        bridge.set_group(group_id, "scene", scene_id)
        return f"Scene '{scene_name}' applied to group '{group_name}'."
    except Exception as e:
        logger.error(f"Error applying scene {scene_id} to group {group_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def find_light_by_name(name: str, ctx: Context) -> str:
    _, light_info = get_bridge_ctx(ctx)
    try:
        name_lower = name.lower()
        matches = {}
        for light_id, light in light_info.items():
            if name_lower in light["name"].lower():
                matches[light_id] = {
                    "id": light_id,
                    "name": light["name"],
                    "type": light["type"],
                    "on": light["state"]["on"],
                }
        if not matches:
            return f"No lights found with name containing '{name}'."
        return json.dumps(matches, indent=2)
    except Exception as e:
        logger.error(f"Error finding lights by name '{name}': {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def create_group(name: str, light_ids: List[int], ctx: Context) -> str:
    bridge, light_info = get_bridge_ctx(ctx)
    try:
        invalid_lights = [lid for lid in light_ids if not validate_light_id(lid, light_info)]
        if invalid_lights:
            return f"Error: Invalid light IDs: {invalid_lights}"
        light_id_strings = [str(lid) for lid in light_ids]
        result = bridge.create_group(name, light_id_strings)
        if "success" in result[0]:
            success_path = list(result[0]["success"].values())[0]
            group_id = success_path.split("/")[-1]
            return f"Group '{name}' created with ID {group_id}, containing {len(light_ids)} lights."
        else:
            return f"Error creating group: {result}"
    except Exception as e:
        logger.error(f"Error creating group '{name}': {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def quick_scene(
    name: str,
    ctx: Context,
    rgb: Optional[List[int]] = None,
    temperature: Optional[int] = None,
    brightness: Optional[int] = None,
    group_id: int = 0
) -> str:
    bridge, _ = get_bridge_ctx(ctx)
    try:
        if not validate_group_id(group_id, bridge):
            return f"Error: Group with ID {group_id} not found."
        group_info = bridge.get_group(group_id)
        group_name = group_info.get("name", f"Group {group_id}")
        bridge.set_group(group_id, "on", True)
        if brightness is not None:
            if not 0 <= brightness <= 254:
                return "Error: Brightness must be between 0 and 254."
            bridge.set_group(group_id, "bri", brightness)
        if rgb is not None:
            if not all(0 <= c <= 255 for c in rgb) or len(rgb) != 3:
                return "Error: RGB values must be three values between 0 and 255."
            xy = rgb_to_xy(rgb[0], rgb[1], rgb[2])
            bridge.set_group(group_id, "xy", xy)
        if temperature is not None:
            if not 2000 <= temperature <= 6500:
                return "Error: Temperature must be between 2000K and 6500K."
            mired = int(1000000 / temperature)
            bridge.set_group(group_id, "ct", mired)

        changes = []
        if brightness is not None:
            changes.append(f"brightness {brightness} ({round((brightness / 254) * 100)}%)")
        if rgb is not None:
            changes.append(f"color RGB({rgb[0]}, {rgb[1]}, {rgb[2]})")
        if temperature is not None:
            changes.append(f"temperature {temperature}K")
        return f"Scene '{name}' applied to group '{group_name}' with {', '.join(changes)}."
    except Exception as e:
        logger.error(f"Error applying quick scene '{name}': {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def refresh_lights(ctx: Context) -> str:
    bridge, _ = get_bridge_ctx(ctx)
    try:
        bridge.get_api()
        light_info = bridge.get_light()
        ctx.request_context.lifespan_context.light_info = light_info
        return f"Refreshed information for {len(light_info)} lights."
    except Exception as e:
        logger.error(f"Error refreshing lights: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def set_color_preset(light_id: int, preset: str, ctx: Context) -> str:
    presets = {
        "warm": {"ct": 2500},
        "cool": {"ct": 4500},
        "daylight": {"ct": 6500},
        "concentration": {"ct": 4600, "bri": 254},
        "relax": {"ct": 2700, "bri": 144},
        "reading": {"ct": 3200, "bri": 219},
        "energize": {"ct": 6000, "bri": 254},
        "red": {"xy": rgb_to_xy(255, 0, 0)},
        "green": {"xy": rgb_to_xy(0, 255, 0)},
        "blue": {"xy": rgb_to_xy(0, 0, 255)},
        "purple": {"xy": rgb_to_xy(128, 0, 128)},
        "orange": {"xy": rgb_to_xy(255, 165, 0)},
    }
    if preset not in presets:
        return f"Error: Unknown preset. Available presets: {', '.join(presets.keys())}"
    bridge, light_info = get_bridge_ctx(ctx)
    try:
        if not validate_light_id(light_id, light_info):
            return f"Error: Light with ID {light_id} not found."
        if "ct" in presets[preset] and "ct" not in light_info[str(light_id)]["state"]:
            return f"Error: Light {light_id} does not support color temperature."
        if "xy" in presets[preset] and "xy" not in light_info[str(light_id)]["state"]:
            return f"Error: Light {light_id} does not support color."
        if not light_info[str(light_id)]["state"]["on"]:
            bridge.set_light(light_id, "on", True)
        for key, value in presets[preset].items():
            bridge.set_light(light_id, key, value)
        return f"Applied '{preset}' preset to light {light_id} ({light_info[str(light_id)]['name']})."
    except Exception as e:
        logger.error(f"Error applying preset '{preset}' to light {light_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def set_group_color_preset(group_id: int, preset: str, ctx: Context) -> str:
    presets = {
        "warm": {"ct": 2500},
        "cool": {"ct": 4500},
        "daylight": {"ct": 6500},
        "concentration": {"ct": 4600, "bri": 254},
        "relax": {"ct": 2700, "bri": 144},
        "reading": {"ct": 3200, "bri": 219},
        "energize": {"ct": 6000, "bri": 254},
        "red": {"xy": rgb_to_xy(255, 0, 0)},
        "green": {"xy": rgb_to_xy(0, 255, 0)},
        "blue": {"xy": rgb_to_xy(0, 0, 255)},
        "purple": {"xy": rgb_to_xy(128, 0, 128)},
        "orange": {"xy": rgb_to_xy(255, 165, 0)},
    }
    if preset not in presets:
        return f"Error: Unknown preset. Available presets: {', '.join(presets.keys())}"
    bridge, _ = get_bridge_ctx(ctx)
    try:
        if not validate_group_id(group_id, bridge):
            return f"Error: Group with ID {group_id} not found."
        group_info = bridge.get_group(group_id)
        group_name = group_info.get("name", f"Group {group_id}")
        if not group_info["state"]["any_on"]:
            bridge.set_group(group_id, "on", True)
        for key, value in presets[preset].items():
            bridge.set_group(group_id, key, value)
        return f"Applied '{preset}' preset to group '{group_name}'."
    except Exception as e:
        logger.error(f"Error applying preset '{preset}' to group {group_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def alert_light(light_id: int, ctx: Context) -> str:
    bridge, light_info = get_bridge_ctx(ctx)
    try:
        if not validate_light_id(light_id, light_info):
            return f"Error: Light with ID {light_id} not found."
        bridge.set_light(light_id, "alert", "select")
        return f"Light {light_id} ({light_info[str(light_id)]['name']}) alerted with a brief flash."
    except Exception as e:
        logger.error(f"Error alerting light {light_id}: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
def set_light_effect(light_id: int, effect: str, ctx: Context) -> str:
    valid_effects = ["none", "colorloop"]
    if effect not in valid_effects:
        return f"Error: Effect must be one of: {', '.join(valid_effects)}"
    bridge, light_info = get_bridge_ctx(ctx)
    try:
        if not validate_light_id(light_id, light_info):
            return f"Error: Light with ID {light_id} not found."
        if "xy" not in light_info[str(light_id)]["state"]:
            return f"Error: Light {light_id} ({light_info[str(light_id)]['name']}) does not support color effects."
        if not light_info[str(light_id)]["state"]["on"]:
            bridge.set_light(light_id, "on", True)
        bridge.set_light(light_id, "effect", effect)
        effect_name = "color loop" if effect == "colorloop" else "no effect"
        return f"Set {effect_name} on light {light_id} ({light_info[str(light_id)]['name']})."
    except Exception as e:
        logger.error(f"Error setting effect {effect} on light {light_id}: {e}")
        return f"Error: {str(e)}"

# --- Prompts ---

@mcp.prompt()
def control_lights() -> str:
    return """
You are connected to a Philips Hue lighting system. I want to control my lights using natural language.
Please help me interpret my requests and use the appropriate tools to control my lighting.

First, if needed, retrieve information about my lights using the resources: hue://lights and hue://groups.
Then, use the appropriate tools to control the lights based on my request.

For example:
- Turn on or off specific lights or groups
- Change brightness or color
- Apply presets for different activities
- Set scenes or effects

Please confirm each action you take and provide feedback on the results.
"""

@mcp.prompt()
def create_mood() -> str:
    return """
You are connected to my Philips Hue lighting system. I want to create mood lighting for a specific activity
or atmosphere. Please help me set up the perfect lighting environment.

First, gather information about my available lights and groups.
Then, suggest and implement a lighting setup based on my mood or activity request.

Consider:
- Appropriate brightness levels for the activity
- Color temperature or colors that match the mood
- Using preset scenes or creating custom settings
- Grouping lights appropriately

After implementing, summarize what you've done and ask if I'd like to make adjustments.
"""

@mcp.prompt()
def light_schedule() -> str:
    return """
I'd like to understand how to set up scheduled lighting with my Philips Hue system. 
Please explain the options available for scheduling automatic lighting changes, 
including:

- Whether scheduling is handled through the Hue app rather than this interface
- The types of schedules I can create (time-based, sunrise/sunset, etc.)
- How to create routines or scenes that can be scheduled
- Any limitations I should be aware of

After explaining the scheduling capabilities, suggest some useful lighting schedules
for typical home use.
"""

# --- JSON Schema helpers for tools/list ---

def python_type_to_schema(t):
    origin, args = get_origin(t), get_args(t)
    if t is str:
        return {"type": "string"}
    if t is int:
        return {"type": "integer"}
    if t is float:
        return {"type": "number"}
    if t is bool:
        return {"type": "boolean"}
    if origin in (list, List, tuple, Tuple):
        item_t = args[0] if args else Any
        return {"type": "array", "items": python_type_to_schema(item_t)}
    if origin in (dict, Dict):
        return {"type": "object"}
    if origin is Union:
        return {"anyOf": [python_type_to_schema(a) for a in args]}
    return {"type": "string"}

def build_schema_from_signature(fn):
    hints = get_type_hints(fn)
    sig = inspect.signature(fn)
    properties, required = {}, []
    for name, p in sig.parameters.items():
        if name == "ctx":
            continue
        properties[name] = python_type_to_schema(hints.get(name, str))
        if p.default is inspect._empty:
            required.append(name)
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema

# --- HTTP Streamable adapter (POST/GET /mcp) ---

GLOBAL_HUE_CTX: Optional[HueContext] = None

@asynccontextmanager
async def http_streamable_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Ouvre la connexion Hue via hue_lifespan et la partage avec les tools.
    """
    global GLOBAL_HUE_CTX
    async with hue_lifespan(mcp) as hue_ctx:
        GLOBAL_HUE_CTX = hue_ctx
        yield
        GLOBAL_HUE_CTX = None

def make_ctx_for_tools() -> Context:
    """
    Construit un 'ctx' minimal compatible avec tes tools (ctx.request_context.lifespan_context).
    """
    class _ReqCtx: ...
    class _Ctx: ...
    rc = _ReqCtx()
    rc.lifespan_context = GLOBAL_HUE_CTX
    c = _Ctx()
    c.request_context = rc
    return c  # objet duck-typed

# Registres simples (nom -> callable)
TOOL_REGISTRY = {
    "get_all_lights": get_all_lights,
    "get_light": get_light,
    "get_all_groups": get_all_groups,
    "get_group": get_group,
    "get_all_scenes": get_all_scenes,
    "turn_on_light": turn_on_light,
    "turn_off_light": turn_off_light,
    "set_brightness": set_brightness,
    "set_color_rgb": set_color_rgb,
    "turn_on_group": turn_on_group,
    "turn_off_group": turn_off_group,
    "set_group_brightness": set_group_brightness,
    "set_group_color_rgb": set_group_color_rgb,
    "set_scene": set_scene,
    "find_light_by_name": find_light_by_name,
    "create_group": create_group,
    "quick_scene": quick_scene,
    "refresh_lights": refresh_lights,
    "set_color_preset": set_color_preset,
    "set_group_color_preset": set_group_color_preset,
    "alert_light": alert_light,
    "set_light_effect": set_light_effect,
}

PROMPT_REGISTRY = {
    "control_lights": control_lights,
    "create_mood": create_mood,
    "light_schedule": light_schedule,
}

def _rpc_error(id_: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}

def _rpc_result(id_: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}

def build_http_app() -> FastAPI:
    app = FastAPI(lifespan=http_streamable_lifespan)

    # CORS (liste d'origines séparées par virgule)
    allowed_origins = []
    if os.getenv("ALLOWED_ORIGINS"):
        allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS").split(",") if o.strip()]
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization", "Mcp-Session-Id"],
            expose_headers=["Mcp-Session-Id"],
        )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp_post(request: Request):
        """
        JSON-RPC over HTTP:
          - Accept: text/event-stream -> renvoie un flux SSE d'événements JSON-RPC
          - Sinon -> JSON unique (ou tableau si batch)
        """
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        accept = request.headers.get("accept", "")
        wants_sse = "text/event-stream" in (accept or "").lower()
        session_id = str(uuid.uuid4())
        extra_headers = {"Mcp-Session-Id": session_id, "Cache-Control": "no-cache"}

        async def handle_one(msg: Dict[str, Any]) -> Dict[str, Any]:
            mid = msg.get("id")
            method = msg.get("method")
            params = msg.get("params") or {}

            # initialize: renvoie la version demandée
            if method == "initialize":
                requested = (params or {}).get("protocolVersion") or "2025-03-26"
                return _rpc_result(mid, {
                    "protocolVersion": requested,
                    "capabilities": {"tools": True, "prompts": True}
                })

            # tools/list: inclut inputSchema
            if method in ("tools/list", "tool/list"):
                tools = []
                for name, fn in TOOL_REGISTRY.items():
                    tools.append({
                        "name": name,
                        "description": (fn.__doc__ or "").strip(),
                        "inputSchema": build_schema_from_signature(fn),
                    })
                return _rpc_result(mid, {"tools": tools})

            # tools/call
            if method in ("tools/call", "tool/call"):
                name = params.get("name")
                arguments = params.get("arguments") or {}
                fn = TOOL_REGISTRY.get(name)
                if not fn:
                    return _rpc_error(mid, -32601, f"Unknown tool '{name}'")
                ctx = make_ctx_for_tools()
                try:
                    sig = inspect.signature(fn)
                    kwargs = {}
                    for pname, p in sig.parameters.items():
                        if pname == "ctx":
                            kwargs["ctx"] = ctx
                        elif pname in arguments:
                            kwargs[pname] = arguments[pname]
                        elif p.default is inspect._empty and pname != "ctx":
                            return _rpc_error(mid, -32602, f"Missing required argument '{pname}' for tool '{name}'")
                    res = fn(**kwargs)
                    if not isinstance(res, str):
                        res = json.dumps(res, ensure_ascii=False)
                    return _rpc_result(mid, {"content": [{"type": "text", "text": res}]})
                except Exception as e:
                    logger.exception("tool call failed")
                    return _rpc_error(mid, -32000, f"Tool '{name}' error: {e}")

            # prompts/list
            if method == "prompts/list":
                prompts = [{"name": name, "description": (fn.__doc__ or "").strip()} for name, fn in PROMPT_REGISTRY.items()]
                return _rpc_result(mid, {"prompts": prompts})

            # prompts/get
            if method == "prompts/get":
                name = params.get("name")
                fn = PROMPT_REGISTRY.get(name)
                if not fn:
                    return _rpc_error(mid, -32601, f"Unknown prompt '{name}'")
                try:
                    text = fn()
                    return _rpc_result(mid, {"name": name, "messages": [{"role": "system", "content": text}]})
                except Exception as e:
                    logger.exception("prompt get failed")
                    return _rpc_error(mid, -32000, f"Prompt '{name}' error: {e}")

            return _rpc_error(mid, -32601, f"Unknown method '{method}'")

        messages = payload if isinstance(payload, list) else [payload]

        if wants_sse:
            async def sse_gen():
                for m in messages:
                    resp = await handle_one(m)
                    yield f"data: {json.dumps(resp, ensure_ascii=False)}\n\n"
            headers = {**extra_headers, "X-Accel-Buffering": "no"}
            return StreamingResponse(sse_gen(), media_type="text/event-stream", headers=headers)

        results = [await handle_one(m) for m in messages]
        body = results if isinstance(payload, list) else results[0]
        return JSONResponse(content=body, headers=extra_headers)

    @app.get("/mcp")
    async def mcp_get():
        """
        Flux SSE optionnel (ping keepalive).
        """
        session_id = str(uuid.uuid4())

        async def ping_stream():
            yield f"data: {{\"jsonrpc\":\"2.0\",\"method\":\"notifications/ping\",\"params\":{{}}}}\n\n"
            while True:
                await asyncio.sleep(25)
                yield f": keepalive\n\n"

        headers = {
            "Mcp-Session-Id": session_id,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(ping_stream(), media_type="text/event-stream", headers=headers)

    # Legacy SSE (compat) monté sous /legacy
    try:
        legacy = mcp.sse_app()
        app.mount("/legacy", legacy)
    except Exception:
        pass

    return app

# --- Main ---

if __name__ == "__main__":
    import uvicorn
    import argparse

    default_host = os.getenv("MCP_HOST", "0.0.0.0")
    default_port = int(os.getenv("MCP_PORT", "8080"))

    parser = argparse.ArgumentParser(description="Philips Hue MCP Server (HTTP Streamable)")
    parser.add_argument("--port", type=int, default=default_port, help="Port to run the server on")
    parser.add_argument("--host", type=str, default=default_host, help="Host to bind the server to")
    parser.add_argument("--log-level", type=str, default="info",
                        choices=["debug", "info", "warning", "error", "critical"],
                        help="Logging level")
    args = parser.parse_args()

    log_level = getattr(logging, args.log_level.upper())
    logging.getLogger("hue-mcp").setLevel(log_level)

    print(f"Starting Philips Hue MCP Server (HTTP streamable) on {args.host}:{args.port}")
    print("Endpoints:")
    print("  POST /mcp   -> JSON-RPC over HTTP (SSE possible si Accept: text/event-stream)")
    print("  GET  /mcp   -> SSE push/keepalive")
    print("  /legacy/... -> Ancien SSE MCP (compat)")

    app = build_http_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
