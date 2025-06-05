#!/usr/bin/env python3
"""
Test Mapper for Mesh Mapper - Arizona Desert Drone Simulation
============================================================

This script simulates 5 drones flying in the Arizona desert for testing and map validation.
It sends realistic detection data to the main mesh-mapper application via HTTP API.

Features:
- 5 distinct drones with unique flight patterns
- Arizona desert coordinates (Sonoran Desert region)
- Realistic altitude variations
- Pilot locations
- Signal strength simulation (RSSI)
- Basic ID and FAA data simulation
- Continuous flight path updates

Usage:
    python test_mapper.py [--host HOST] [--port PORT] [--duration MINUTES]
"""

import json
import time
import random
import requests
import argparse
import threading
from datetime import datetime
from typing import Dict, List, Tuple
import math

# Arizona Desert Test Area - Sonoran Desert coordinates
# Centered around Phoenix/Scottsdale area
ARIZONA_DESERT_CENTER = {
    'lat': 33.4942,  # Phoenix area
    'lng': -111.9261
}

# Test area bounds (roughly 50km x 50km area)
TEST_AREA_BOUNDS = {
    'north': 33.7,
    'south': 33.3,
    'east': -111.7,
    'west': -112.2
}

class DroneSimulator:
    """Simulates a single drone's flight pattern in the Arizona desert"""
    
    def __init__(self, drone_id: int, mac_address: str, basic_id: str):
        self.drone_id = drone_id
        self.mac_address = mac_address
        self.basic_id = basic_id
        self.current_position = self._generate_start_position()
        self.target_position = self._generate_target_position()
        self.pilot_position = self._generate_pilot_position()
        self.altitude = random.randint(50, 400)  # FAA allowed range
        self.speed = random.uniform(5, 25)  # m/s (roughly 11-56 mph)
        self.direction = random.uniform(0, 360)  # degrees
        self.last_update = time.time()
        self.flight_pattern = self._choose_flight_pattern()
        
        # FAA data simulation
        self.faa_data = self._generate_faa_data()
        
    def _generate_start_position(self) -> Dict[str, float]:
        """Generate a random starting position within Arizona desert bounds"""
        lat = random.uniform(TEST_AREA_BOUNDS['south'], TEST_AREA_BOUNDS['north'])
        lng = random.uniform(TEST_AREA_BOUNDS['west'], TEST_AREA_BOUNDS['east'])
        return {'lat': lat, 'lng': lng}
    
    def _generate_target_position(self) -> Dict[str, float]:
        """Generate a target position for flight pattern"""
        lat = random.uniform(TEST_AREA_BOUNDS['south'], TEST_AREA_BOUNDS['north'])
        lng = random.uniform(TEST_AREA_BOUNDS['west'], TEST_AREA_BOUNDS['east'])
        return {'lat': lat, 'lng': lng}
    
    def _generate_pilot_position(self) -> Dict[str, float]:
        """Generate pilot position (usually stationary, within reasonable distance)"""
        # Pilot typically within 5km of drone start position
        offset_lat = random.uniform(-0.045, 0.045)  # ~5km
        offset_lng = random.uniform(-0.045, 0.045)
        
        pilot_lat = self.current_position['lat'] + offset_lat
        pilot_lng = self.current_position['lng'] + offset_lng
        
        # Ensure pilot stays within bounds
        pilot_lat = max(TEST_AREA_BOUNDS['south'], min(TEST_AREA_BOUNDS['north'], pilot_lat))
        pilot_lng = max(TEST_AREA_BOUNDS['west'], min(TEST_AREA_BOUNDS['east'], pilot_lng))
        
        return {'lat': pilot_lat, 'lng': pilot_lng}
    
    def _choose_flight_pattern(self) -> str:
        """Choose a flight pattern for this drone"""
        patterns = ['linear', 'circular', 'waypoint', 'search_pattern', 'hover']
        return random.choice(patterns)
    
    def _generate_faa_data(self) -> Dict:
        """Generate realistic FAA registration data"""
        manufacturers = ["DJI", "Autel", "Parrot", "Skydio", "Yuneec"]
        models = ["Mavic 3", "Air 2S", "Mini 3 Pro", "Phantom 4", "Inspire 2", "EVO II", "ANAFI", "X2"]
        
        return {
            "registrant_name": f"Test Pilot {self.drone_id}",
            "registrant_type": "Individual",
            "manufacturer": random.choice(manufacturers),
            "model": random.choice(models),
            "registration_date": "2023-01-15",
            "expiration_date": "2026-01-15",
            "status": "Active",
            "serial_number": f"TST{self.drone_id:03d}{random.randint(1000, 9999)}",
            "weight": random.uniform(0.5, 25.0),  # kg
            "purpose": random.choice(["Recreation", "Commercial", "Educational", "Research"])
        }
    
    def _calculate_distance(self, pos1: Dict[str, float], pos2: Dict[str, float]) -> float:
        """Calculate distance between two positions in meters"""
        lat1, lng1 = math.radians(pos1['lat']), math.radians(pos1['lng'])
        lat2, lng2 = math.radians(pos2['lat']), math.radians(pos2['lng'])
        
        dlat = lat2 - lat1
        dlng = lng2 - lng1
        
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng/2)**2
        c = 2 * math.asin(math.sqrt(a))
        
        return 6371000 * c  # Earth radius in meters
    
    def update_position(self):
        """Update drone position based on flight pattern"""
        current_time = time.time()
        dt = current_time - self.last_update
        self.last_update = current_time
        
        if self.flight_pattern == 'linear':
            self._update_linear_flight(dt)
        elif self.flight_pattern == 'circular':
            self._update_circular_flight(dt)
        elif self.flight_pattern == 'waypoint':
            self._update_waypoint_flight(dt)
        elif self.flight_pattern == 'search_pattern':
            self._update_search_pattern(dt)
        elif self.flight_pattern == 'hover':
            self._update_hover_pattern(dt)
        
        # Update altitude with small variations
        self.altitude += random.uniform(-2, 2)
        self.altitude = max(30, min(400, self.altitude))  # Keep within legal limits
    
    def _update_linear_flight(self, dt: float):
        """Linear flight pattern - fly towards target"""
        distance_to_target = self._calculate_distance(self.current_position, self.target_position)
        
        if distance_to_target < 100:  # Within 100m of target, choose new target
            self.target_position = self._generate_target_position()
            return
        
        # Calculate movement
        lat_diff = self.target_position['lat'] - self.current_position['lat']
        lng_diff = self.target_position['lng'] - self.current_position['lng']
        
        # Normalize and apply speed
        distance = math.sqrt(lat_diff**2 + lng_diff**2)
        if distance > 0:
            move_lat = (lat_diff / distance) * (self.speed * dt) / 111000  # Convert m to degrees
            move_lng = (lng_diff / distance) * (self.speed * dt) / (111000 * math.cos(math.radians(self.current_position['lat'])))
            
            self.current_position['lat'] += move_lat
            self.current_position['lng'] += move_lng
    
    def _update_circular_flight(self, dt: float):
        """Circular flight pattern"""
        radius = 0.01  # ~1km radius in degrees
        angular_speed = self.speed / (radius * 111000)  # Convert to angular velocity
        
        center_lat = ARIZONA_DESERT_CENTER['lat']
        center_lng = ARIZONA_DESERT_CENTER['lng']
        
        # Calculate current angle
        current_angle = math.atan2(
            self.current_position['lng'] - center_lng,
            self.current_position['lat'] - center_lat
        )
        
        # Update angle
        current_angle += angular_speed * dt
        
        # Calculate new position
        self.current_position['lat'] = center_lat + radius * math.cos(current_angle)
        self.current_position['lng'] = center_lng + radius * math.sin(current_angle)
    
    def _update_waypoint_flight(self, dt: float):
        """Waypoint-based flight pattern"""
        # Similar to linear but with multiple waypoints
        self._update_linear_flight(dt)
        
        # Randomly change direction occasionally
        if random.random() < 0.01:  # 1% chance per update
            self.target_position = self._generate_target_position()
    
    def _update_search_pattern(self, dt: float):
        """Search/survey pattern (back and forth)"""
        # Implement a lawn-mower pattern
        move_distance = self.speed * dt / 111000  # Convert to degrees
        
        # Move in current direction
        self.current_position['lat'] += move_distance * math.cos(math.radians(self.direction))
        self.current_position['lng'] += move_distance * math.sin(math.radians(self.direction))
        
        # Check bounds and reverse direction if needed
        if (self.current_position['lat'] >= TEST_AREA_BOUNDS['north'] or 
            self.current_position['lat'] <= TEST_AREA_BOUNDS['south'] or
            self.current_position['lng'] >= TEST_AREA_BOUNDS['east'] or
            self.current_position['lng'] <= TEST_AREA_BOUNDS['west']):
            
            self.direction = (self.direction + 180) % 360
    
    def _update_hover_pattern(self, dt: float):
        """Hovering pattern with small movements"""
        # Small random movements around current position
        move_distance = random.uniform(-0.0001, 0.0001)  # Very small movements
        self.current_position['lat'] += move_distance
        self.current_position['lng'] += move_distance
    
    def generate_detection(self) -> Dict:
        """Generate a detection object for this drone"""
        current_time = time.time()
        
        # Simulate RSSI based on distance from pilot
        pilot_distance = self._calculate_distance(self.current_position, self.pilot_position)
        # RSSI typically -30 to -90 dBm, closer = stronger signal
        base_rssi = -40
        distance_factor = min(pilot_distance / 1000, 50)  # Max 50km consideration
        rssi = base_rssi - (distance_factor * 1.0) + random.uniform(-5, 5)
        rssi = max(-90, min(-30, rssi))  # Clamp to realistic range
        
        detection = {
            "mac": self.mac_address,
            "rssi": round(rssi, 1),
            "drone_lat": round(self.current_position['lat'], 6),
            "drone_long": round(self.current_position['lng'], 6),
            "drone_altitude": round(self.altitude, 1),
            "pilot_lat": round(self.pilot_position['lat'], 6),
            "pilot_long": round(self.pilot_position['lng'], 6),
            "basic_id": self.basic_id,
            "faa_data": self.faa_data,
            "last_update": current_time,
            "status": "active"
        }
        
        return detection

class ArizonaDesertTestSuite:
    """Main test suite for Arizona desert drone simulation"""
    
    def __init__(self, host: str = 'localhost', port: int = 5000):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.drones = []
        self.running = False
        
        # Initialize 5 test drones
        self._initialize_drones()
    
    def _initialize_drones(self):
        """Initialize 5 test drones with unique configurations"""
        drone_configs = [
            {"id": 1, "mac": "AA:BB:CC:DD:EE:01", "basic_id": "AZTEST001", "name": "Desert Eagle"},
            {"id": 2, "mac": "AA:BB:CC:DD:EE:02", "basic_id": "AZTEST002", "name": "Cactus Hawk"},
            {"id": 3, "mac": "AA:BB:CC:DD:EE:03", "basic_id": "AZTEST003", "name": "Saguaro Scout"},
            {"id": 4, "mac": "AA:BB:CC:DD:EE:04", "basic_id": "AZTEST004", "name": "Mesa Phantom"},
            {"id": 5, "mac": "AA:BB:CC:DD:EE:05", "basic_id": "AZTEST005", "name": "Sonoran Surveyor"}
        ]
        
        for config in drone_configs:
            drone = DroneSimulator(config["id"], config["mac"], config["basic_id"])
            drone.name = config["name"]
            self.drones.append(drone)
            print(f"Initialized {config['name']} (MAC: {config['mac']}) at "
                  f"lat {drone.current_position['lat']:.6f}, lng {drone.current_position['lng']:.6f}")
    
    def test_connection(self) -> bool:
        """Test connection to the mesh-mapper API"""
        try:
            response = requests.get(f"{self.base_url}/api/detections", timeout=5)
            if response.status_code == 200:
                print(f"✓ Successfully connected to mesh-mapper at {self.base_url}")
                return True
            else:
                print(f"✗ Connection failed: HTTP {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            print(f"✗ Connection failed: {e}")
            return False
    
    def send_detection(self, detection: Dict) -> bool:
        """Send a detection to the mesh-mapper API"""
        try:
            response = requests.post(
                f"{self.base_url}/api/detections",
                json=detection,
                headers={'Content-Type': 'application/json'},
                timeout=5
            )
            return response.status_code == 200
        except requests.exceptions.RequestException as e:
            print(f"Failed to send detection: {e}")
            return False
    
    def drone_simulation_thread(self, drone: DroneSimulator, update_interval: float):
        """Thread function for individual drone simulation"""
        print(f"Started simulation thread for {drone.name}")
        
        while self.running:
            try:
                # Update drone position
                drone.update_position()
                
                # Generate and send detection
                detection = drone.generate_detection()
                success = self.send_detection(detection)
                
                if success:
                    print(f"🛩️  {drone.name}: lat {detection['drone_lat']:.6f}, "
                          f"lng {detection['drone_long']:.6f}, alt {detection['drone_altitude']:.1f}m, "
                          f"RSSI {detection['rssi']:.1f}dBm")
                else:
                    print(f"⚠️  Failed to send detection for {drone.name}")
                
                time.sleep(update_interval)
                
            except Exception as e:
                print(f"Error in drone simulation for {drone.name}: {e}")
                time.sleep(1)
        
        print(f"Stopped simulation thread for {drone.name}")
    
    def start_simulation(self, duration_minutes: float = 60, update_interval: float = 2.0):
        """Start the Arizona desert drone simulation"""
        if not self.test_connection():
            print("Cannot start simulation - connection test failed")
            return False
        
        print(f"\n🏜️  Starting Arizona Desert Drone Simulation")
        print(f"   Duration: {duration_minutes} minutes")
        print(f"   Update interval: {update_interval} seconds")
        print(f"   Test area: {TEST_AREA_BOUNDS}")
        print(f"   Number of drones: {len(self.drones)}")
        print("="*60)
        
        self.running = True
        threads = []
        
        # Start simulation thread for each drone
        for drone in self.drones:
            thread = threading.Thread(
                target=self.drone_simulation_thread,
                args=(drone, update_interval),
                daemon=True
            )
            thread.start()
            threads.append(thread)
        
        try:
            # Run for specified duration
            time.sleep(duration_minutes * 60)
        except KeyboardInterrupt:
            print("\n🛑 Simulation interrupted by user")
        
        # Stop simulation
        print("\n🏁 Stopping simulation...")
        self.running = False
        
        # Wait for threads to finish
        for thread in threads:
            thread.join(timeout=5)
        
        print("✓ Arizona Desert Drone Simulation completed")
        return True
    
    def generate_summary_report(self):
        """Generate a summary report of the test configuration"""
        print("\n" + "="*60)
        print("ARIZONA DESERT DRONE TEST CONFIGURATION")
        print("="*60)
        print(f"Test Area: Arizona Sonoran Desert")
        print(f"Center: {ARIZONA_DESERT_CENTER['lat']:.6f}, {ARIZONA_DESERT_CENTER['lng']:.6f}")
        print(f"Bounds: N{TEST_AREA_BOUNDS['north']:.3f} S{TEST_AREA_BOUNDS['south']:.3f} "
              f"E{TEST_AREA_BOUNDS['east']:.3f} W{TEST_AREA_BOUNDS['west']:.3f}")
        print(f"Area Size: ~50km x 50km")
        print()
        
        for i, drone in enumerate(self.drones, 1):
            print(f"Drone {i}: {drone.name}")
            print(f"  MAC: {drone.mac_address}")
            print(f"  Basic ID: {drone.basic_id}")
            print(f"  Start Position: {drone.current_position['lat']:.6f}, {drone.current_position['lng']:.6f}")
            print(f"  Pilot Position: {drone.pilot_position['lat']:.6f}, {drone.pilot_position['lng']:.6f}")
            print(f"  Flight Pattern: {drone.flight_pattern}")
            print(f"  FAA Registration: {drone.faa_data['manufacturer']} {drone.faa_data['model']}")
            print()

def main():
    """Main function with command line interface"""
    parser = argparse.ArgumentParser(
        description="Arizona Desert Drone Simulation for Mesh Mapper Testing"
    )
    parser.add_argument(
        '--host', 
        default='localhost',
        help='Mesh-mapper host (default: localhost)'
    )
    parser.add_argument(
        '--port', 
        type=int, 
        default=5000,
        help='Mesh-mapper port (default: 5000)'
    )
    parser.add_argument(
        '--duration', 
        type=float, 
        default=30,
        help='Simulation duration in minutes (default: 30)'
    )
    parser.add_argument(
        '--interval', 
        type=float, 
        default=2.0,
        help='Update interval in seconds (default: 2.0)'
    )
    parser.add_argument(
        '--summary-only', 
        action='store_true',
        help='Show configuration summary only, do not run simulation'
    )
    
    args = parser.parse_args()
    
    # Create test suite
    test_suite = ArizonaDesertTestSuite(args.host, args.port)
    
    # Show configuration summary
    test_suite.generate_summary_report()
    
    if args.summary_only:
        print("Summary only mode - simulation not started")
        return
    
    # Start simulation
    success = test_suite.start_simulation(args.duration, args.interval)
    
    if success:
        print("\n✅ Test completed successfully!")
        print("Check the mesh-mapper web interface to view the simulated drone data.")
    else:
        print("\n❌ Test failed - check connection and try again.")

if __name__ == "__main__":
    main() 
