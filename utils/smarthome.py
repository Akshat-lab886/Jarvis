"""
Jarvis Smart Home Module
Virtual simulation of smart home devices for voice control.
"""

import logging

logger = logging.getLogger("Jarvis.SmartHome")


class SmartHome:
    """
    Manages a virtual smart home with controllable devices.
    This is a simulation - can be extended to control real devices 
    via HomeAssistant, SmartThings, or other APIs.
    """
    
    def __init__(self):
        """Initialize the smart home with default devices."""
        logger.info("SmartHome module initialized")
        
        # Device registry - can be extended with more device types
        self.devices = {
            "living_room_light": {
                "name": "Living Room Light",
                "type": "light",
                "status": "off",
                "brightness": 100
            },
            "bedroom_light": {
                "name": "Bedroom Light", 
                "type": "light",
                "status": "off",
                "brightness": 100
            },
            "kitchen_light": {
                "name": "Kitchen Light",
                "type": "light",
                "status": "off",
                "brightness": 100
            },
            "bathroom_light": {
                "name": "Bathroom Light",
                "type": "light", 
                "status": "off",
                "brightness": 100
            },
            "fan": {
                "name": "Fan",
                "type": "fan",
                "status": "off",
                "speed": 3  # 1-5
            },
            "ac": {
                "name": "Air Conditioner",
                "type": "ac",
                "status": "off",
                "temperature": 24
            }
        }
        
        # Aliases for fuzzy matching
        self.aliases = {
            "living room": "living_room_light",
            "living room light": "living_room_light",
            "lounge": "living_room_light",
            "bedroom": "bedroom_light",
            "bedroom light": "bedroom_light",
            "kitchen": "kitchen_light",
            "kitchen light": "kitchen_light",
            "bathroom": "bathroom_light",
            "bathroom light": "bathroom_light",
            "washroom": "bathroom_light",
            "fan": "fan",
            "ceiling fan": "fan",
            "ac": "ac",
            "air conditioner": "ac",
            "air conditioning": "ac",
            "all lights": "all_lights",
            "all": "all_devices"
        }

        # Restore persisted state (if any)
        self._load()
    
    def _find_device(self, device_name):
        """Find device by name or alias (fuzzy matching)."""
        device_name = device_name.lower().strip()
        
        # Direct match
        if device_name in self.devices:
            return device_name
        
        # Alias match
        if device_name in self.aliases:
            return self.aliases[device_name]
        
        # Partial match
        for alias, device_id in self.aliases.items():
            if alias in device_name or device_name in alias:
                return device_id                # Try matching device names
        for device_id, device in self.devices.items():
            if device_name in device["name"].lower():
                return device_id
        
        return None
    
    # ------------------------------------------------------------------ #
    # Persistence (device state survives restarts)
    # ------------------------------------------------------------------ #
    def _save(self):
        import json
        import os
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base_dir, 'smarthome.json')
        try:
            with open(path, 'w') as f:
                json.dump(self.devices, f, indent=2)
        except Exception as e:
            logger.warning("SmartHome: Failed to save state: %s", e)

    def _load(self):
        import json
        import os
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base_dir, 'smarthome.json')
        try:
            if os.path.exists(path):
                with open(path, 'r') as f:
                    saved = json.load(f)
                for key, data in saved.items():
                    if key in self.devices and isinstance(data, dict):
                        self.devices[key].update(data)
        except Exception as e:
            logger.warning("SmartHome: Failed to load state: %s", e)

    def control_device(self, device_name, action, value=None):
        """
        Control a smart home device (persists the new state).
        
        Args:
            device_name: Name or alias of the device
            action: "turn_on", "turn_off", "set_brightness", "set_temperature", "set_speed"
            value: Optional value for brightness (0-100), temperature, or speed
            
        Returns:
            str: Confirmation message
        """
        result = self._control_device_impl(device_name, action, value)
        self._save()
        return result

    def _control_device_impl(self, device_name, action, value=None):
        """Original control logic (state mutation only)."""
        action = action.lower().strip()
        
        # Handle "all lights" command
        if device_name.lower() in ["all lights", "all_lights"]:
            return self._control_all_lights(action)
        
        # Handle "all devices" command
        if device_name.lower() in ["all", "all_devices", "everything"]:
            return self._control_all_devices(action)
        
        device_id = self._find_device(device_name)
        
        if not device_id:
            return f"I couldn't find a device called '{device_name}'. Available devices: {', '.join([d['name'] for d in self.devices.values()])}"
        
        device = self.devices[device_id]
        device_friendly_name = device["name"]
        
        # Handle different actions
        if action in ["turn_on", "on", "switch_on"]:
            device["status"] = "on"
            return f"{device_friendly_name} turned ON."
        
        elif action in ["turn_off", "off", "switch_off"]:
            device["status"] = "off"
            return f"{device_friendly_name} turned OFF."
        
        elif action in ["toggle"]:
            device["status"] = "on" if device["status"] == "off" else "off"
            return f"{device_friendly_name} toggled to {device['status'].upper()}."
        
        elif action in ["set_brightness", "brightness", "dim"]:
            if device["type"] != "light":
                return f"{device_friendly_name} doesn't support brightness control."
            
            brightness = int(value) if value else 50
            brightness = max(0, min(100, brightness))  # Clamp 0-100
            device["brightness"] = brightness
            device["status"] = "on" if brightness > 0 else "off"
            return f"{device_friendly_name} brightness set to {brightness}%."
        
        elif action in ["set_temperature", "temperature", "set_temp"]:
            if device["type"] != "ac":
                return f"{device_friendly_name} doesn't support temperature control."
            
            temp = int(value) if value else 24
            temp = max(16, min(30, temp))  # Clamp 16-30
            device["temperature"] = temp
            device["status"] = "on"
            return f"{device_friendly_name} set to {temp}°C."
        
        elif action in ["set_speed", "speed"]:
            if device["type"] != "fan":
                return f"{device_friendly_name} doesn't support speed control."
            
            speed = int(value) if value else 3
            speed = max(1, min(5, speed))  # Clamp 1-5
            device["speed"] = speed
            device["status"] = "on"
            return f"{device_friendly_name} speed set to {speed}."
        
        else:
            return f"Unknown action '{action}'. Try: turn_on, turn_off, set_brightness, set_temperature, set_speed."
    
    def _control_all_lights(self, action):
        """Control all lights at once."""
        lights = [d for d_id, d in self.devices.items() if d["type"] == "light"]
        
        if action in ["turn_on", "on", "switch_on"]:
            for d_id, device in self.devices.items():
                if device["type"] == "light":
                    device["status"] = "on"
            return f"All {len(lights)} lights turned ON."
        
        elif action in ["turn_off", "off", "switch_off"]:
            for d_id, device in self.devices.items():
                if device["type"] == "light":
                    device["status"] = "off"
            return f"All {len(lights)} lights turned OFF."
        
        return "I can turn all lights on or off."
    
    def _control_all_devices(self, action):
        """Control all devices at once."""
        if action in ["turn_on", "on", "switch_on"]:
            for device in self.devices.values():
                device["status"] = "on"
            return f"All {len(self.devices)} devices turned ON."
        
        elif action in ["turn_off", "off", "switch_off"]:
            for device in self.devices.values():
                device["status"] = "off"
            return f"All {len(self.devices)} devices turned OFF."
        
        return "I can turn all devices on or off."
    
    def get_status(self, device_name=None):
        """
        Get status of devices.
        
        Args:
            device_name: Optional - specific device, or None for all
            
        Returns:
            str: Status summary
        """
        if device_name:
            device_id = self._find_device(device_name)
            if device_id:
                device = self.devices[device_id]
                status = device["status"].upper()
                extra = ""
                if device["type"] == "light" and device["status"] == "on":
                    extra = f" at {device['brightness']}% brightness"
                elif device["type"] == "ac" and device["status"] == "on":
                    extra = f" at {device['temperature']}°C"
                elif device["type"] == "fan" and device["status"] == "on":
                    extra = f" at speed {device['speed']}"
                return f"{device['name']} is {status}{extra}."
            return f"Device '{device_name}' not found."
        
        # Return all device statuses
        status_parts = []
        for device_id, device in self.devices.items():
            status = "🟢 ON" if device["status"] == "on" else "⚫ OFF"
            status_parts.append(f"{device['name']}: {status}")
        
        return "Smart Home Status:\n" + "\n".join(status_parts)
    
    def get_devices_json(self):
        """Get all devices as JSON for the dashboard."""
        return self.devices


# Test
if __name__ == "__main__":
    home = SmartHome()
    print(home.control_device("living room", "turn_on"))
    print(home.control_device("bedroom light", "set_brightness", 50))
    print(home.control_device("ac", "set_temperature", 22))
    print(home.get_status())
