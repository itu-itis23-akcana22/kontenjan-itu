#!/usr/bin/env python3
"""
Script to empty all subscription lists in subscriptions.json
Keeps all user IDs (keys) but clears their subscription lists
"""

import json
import os

def empty_subscription_lists():
    """Empty all subscription lists while keeping the keys"""
    
    # Path to the subscriptions file
    subscriptions_file = "subscriptions.json"
    
    # Check if file exists
    if not os.path.exists(subscriptions_file):
        print(f"Error: {subscriptions_file} not found!")
        return
    
    try:
        # Read the current subscriptions
        with open(subscriptions_file, 'r', encoding='utf-8') as file:
            subscriptions = json.load(file)
        
        print(f"Found {len(subscriptions)} users in subscriptions file")
        
        # Count how many users had non-empty lists before clearing
        non_empty_count = sum(1 for user_id, subs in subscriptions.items() if subs)
        
        # Empty all lists while keeping keys
        for user_id in subscriptions:
            subscriptions[user_id] = []
        
        # Write back to file
        with open(subscriptions_file, 'w', encoding='utf-8') as file:
            json.dump(subscriptions, file, indent=0, separators=(',', ': '))
        
        print(f"Successfully cleared subscription lists for {non_empty_count} users")
        print(f"All {len(subscriptions)} user IDs preserved with empty lists")
        
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON format in {subscriptions_file}")
        print(f"Details: {e}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    empty_subscription_lists()