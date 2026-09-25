import math

def validate_recipe(recipe):
    for field in ('start', 'end', 'overlay_seconds'):
        value = float(recipe.get(field, 3 if field == 'overlay_seconds' else 0))
        if not math.isfinite(value):
            raise ValueError(f'{field} must be a finite number')
        recipe[field] = value
    if recipe['start'] < 0 or recipe['end'] <= recipe['start']:
        raise ValueError('End must be after start; start cannot be negative.')
    if not 0 <= recipe['overlay_seconds'] <= 60:
        raise ValueError('Overlay duration must be between 0 and 60 seconds.')
    for field, allowed in {'aspect': ('9:16','16:9','1:1','4:3','4:5'),
                           'track': ('off','auto','lock'), 'layout': ('follow','center','split2')}.items():
        if recipe.get(field) not in allowed:
            raise ValueError(f'Unsupported {field}')
    if not isinstance(recipe.get('captions_enabled'), bool):
        raise ValueError('Captions must be enabled or disabled.')
    if not isinstance(recipe.get('overlay_text', ''), str) or len(recipe.get('overlay_text','')) > 160:
        raise ValueError('Overlay title must contain at most 160 characters.')
    return recipe
