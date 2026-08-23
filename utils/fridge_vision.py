"""
Jarvis Fridge Vision & Recipe Engine

Computer vision integration that:
- Takes a photo of a messy fridge/pantry
- Uses the vision model to identify ingredients
- Maps out available ingredients
- Suggests recipes based on what's available
- Generates a shopping list for missing items
- Considers dietary restrictions and preferences from episodic memory

Trigger: "what can I cook?" / "look at my fridge" / "recipe suggestions"
"""

import os
import json
import datetime
import logging

logger = logging.getLogger("Jarvis.FridgeVision")


class FridgeVision:
    """
    Analyzes photos of food/fridge contents and generates recipes.
    """

    def __init__(self, brain=None, episodic_memory=None):
        self.brain = brain
        self.memory = episodic_memory
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.static_dir = os.path.join(self.base_dir, 'static')

    def analyze_fridge(self, photo_path=None, user_prompt=None):
        """
        Analyze a fridge/pantry photo and suggest recipes.

        Args:
            photo_path: Path to the image (default: webcam_capture.jpg)
            user_prompt: Additional context from user

        Returns:
            dict with keys: ingredients, recipes, shopping_list
        """
        if not photo_path:
            photo_path = os.path.join(self.static_dir, 'webcam_capture.jpg')

        if not os.path.exists(photo_path):
            return {
                'error': "No photo found. Please take a photo of your fridge first.",
                'ingredients': [],
                'recipes': [],
                'shopping_list': []
            }

        if not self.brain:
            return {
                'error': "Brain not available for image analysis.",
                'ingredients': [],
                'recipes': [],
                'shopping_list': []
            }

        # Build context from episodic memory (dietary preferences, allergies)
        diet_context = self._get_dietary_context()

        # Step 1: Use vision model to identify ingredients
        prompt = (
            "Analyze this photo of a fridge/pantry/kitchen. "
            "Identify ALL visible food items and ingredients.\n\n"
            "Return a JSON object with:\n"
            '{"ingredients": ["item1", "item2", ...], '
            '"category": {"proteins": [...], "vegetables": [...], '
            '"dairy": [...], "grains": [...], "condiments": [...]}}\n\n'
            "List every identifiable food item, even partially visible ones."
        )

        if diet_context:
            prompt += f"\n\nUser dietary info: {diet_context}"

        try:
            result = self.brain.think(prompt, image_path=photo_path)
            response_text = result.get('response', '')

            # Try to parse JSON from response
            ingredients_data = self._parse_ingredients(response_text)

            if not ingredients_data.get('ingredients'):
                # Fallback: treat the whole response as ingredient list
                ingredients_data['ingredients'] = [
                    line.strip().lstrip('-•*').strip()
                    for line in response_text.split('\n')
                    if line.strip() and len(line.strip()) > 2
                ][:20]

        except Exception as e:
            logger.error(f"Ingredient analysis error: {e}")
            return {
                'error': f"Failed to analyze the photo: {str(e)}",
                'ingredients': [],
                'recipes': [],
                'shopping_list': []
            }

        # Step 2: Generate recipe suggestions
        ingredients = ingredients_data.get('ingredients', [])
        recipes = self._suggest_recipes(ingredients, diet_context)

        # Step 3: Generate shopping list for common missing items
        shopping_list = self._generate_shopping_list(ingredients, recipes)

        # Store in episodic memory
        if self.memory:
            self.memory.remember(
                text=f"Fridge scan: found {len(ingredients)} items. "
                     f"Suggested {len(recipes)} recipes.",
                category='context',
                tags=['fridge-scan', 'cooking', 'auto-captured'],
                source='passive',
                importance=3,
                metadata={
                    'ingredient_count': len(ingredients),
                    'recipe_count': len(recipes)
                }
            )

        return {
            'ingredients': ingredients,
            'ingredient_categories': ingredients_data.get('category', {}),
            'recipes': recipes,
            'shopping_list': shopping_list
        }

    def _get_dietary_context(self):
        """Get dietary preferences and restrictions from episodic memory."""
        if not self.memory:
            return ""

        context_parts = []

        # Search for health/diet related memories
        health = self.memory.get_by_category('health', limit=5)
        for h in health:
            text = h.get('text', '').lower()
            if any(kw in text for kw in ['vegan', 'vegetarian', 'keto', 'allergic',
                                          'diet', 'gluten', 'lactose', 'diabetic']):
                context_parts.append(h['text'])

        # Search for food preferences
        prefs = self.memory.get_by_category('preference', limit=10)
        for p in prefs:
            text = p.get('text', '').lower()
            if any(kw in text for kw in ['food', 'eat', 'cook', 'cuisine', 'meal',
                                          'restaurant', 'dish', 'recipe']):
                context_parts.append(p['text'])

        # Search relationship details for family allergies
        rels = self.memory.get_by_category('relationship', limit=10)
        for r in rels:
            text = r.get('text', '').lower()
            if 'allerg' in text or 'intoleran' in text:
                context_parts.append(r['text'])

        return "; ".join(context_parts) if context_parts else ""

    def _parse_ingredients(self, text):
        """Try to extract ingredient list from the AI response."""
        import re

        # Try JSON extraction
        json_match = re.search(r'\{[^{}]*"ingredients"[^{}]*\}', text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                return data
            except json.JSONDecodeError:
                pass

        # Try markdown list
        ingredients = []
        lines = text.split('\n')
        in_list = False
        for line in lines:
            line = line.strip()
            if 'ingredient' in line.lower():
                in_list = True
                continue
            if in_list and (line.startswith('-') or line.startswith('*') or line.startswith('•')):
                item = line.lstrip('-*•').strip()
                if item:
                    ingredients.append(item)
            elif in_list and line and not line.startswith('-'):
                break

        # Fallback: extract any quoted or comma-separated items
        if not ingredients:
            # Look for quoted items
            quoted = re.findall(r'"([^"]+)"', text)
            if quoted:
                ingredients = quoted
            else:
                # Comma-separated
                parts = text.split(',')
                ingredients = [p.strip().lstrip('-*•').strip() for p in parts if p.strip()]

        return {'ingredients': ingredients[:30]}

    def _suggest_recipes(self, ingredients, dietary_context=""):
        """Suggest recipes based on available ingredients."""
        if not self.brain or not ingredients:
            return []

        ingredient_str = ", ".join(ingredients[:20])

        prompt = (
            f"I have these ingredients: {ingredient_str}\n\n"
            f"{'Dietary restrictions: ' + dietary_context if dietary_context else ''}\n\n"
            "Suggest 3-5 recipes I can make with these ingredients. "
            "For each recipe, provide:\n"
            '- name: recipe name\n'
            '- time: estimated cooking time\n'
            '- difficulty: easy/medium/hard\n'
            '- ingredients_used: which of my ingredients it uses\n'
            '- missing: any ingredients I might need that aren\'t listed\n'
            '- steps: brief cooking steps\n\n'
            "Return as a JSON array of recipe objects."
        )

        try:
            result = self.brain.think(prompt)
            response = result.get('response', '')

            # Try to parse JSON
            import re
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if json_match:
                recipes = json.loads(json_match.group())
                if isinstance(recipes, list):
                    return recipes[:5]

            # Fallback: return the text as-is formatted
            return [{'name': 'Suggested Recipes', 'steps': response[:500]}]

        except Exception as e:
            logger.error(f"Recipe suggestion error: {e}")
            return [{'name': 'Recipe suggestions', 'steps': 'Could not generate recipes at this time.'}]

    def _generate_shopping_list(self, ingredients, recipes):
        """Generate a shopping list for common items not in the fridge."""
        # Common staples that most recipes need
        common_staples = [
            'salt', 'pepper', 'oil', 'butter', 'garlic',
            'onion', 'rice', 'pasta', 'bread', 'eggs',
            'milk', 'cheese', 'sugar', 'flour'
        ]

        ingredients_lower = {i.lower() for i in ingredients}
        shopping = []

        for staple in common_staples:
            if not any(staple in ing for ing in ingredients_lower):
                shopping.append(staple)

        # Add any missing ingredients from recipes
        for recipe in recipes:
            missing = recipe.get('missing', [])
            if isinstance(missing, list):
                for item in missing:
                    if item and not any(item.lower() in ing for ing in ingredients_lower):
                        if item.lower() not in [s.lower() for s in shopping]:
                            shopping.append(item)

        return shopping

    def quick_recipe(self, prompt_text):
        """
        Get a recipe suggestion without a photo.
        Just based on text input like "what can I make with chicken and rice?"
        """
        if not self.brain:
            return "Brain not available for recipe generation."

        # Add dietary context
        diet = self._get_dietary_context()
        full_prompt = prompt_text
        if diet:
            full_prompt += f"\n\nDietary context: {diet}"

        try:
            result = self.brain.think(full_prompt)
            return result.get('response', "I couldn't think of a recipe right now.")
        except Exception as e:
            return f"Recipe generation failed: {str(e)}"
