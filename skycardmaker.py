import re
import requests
from PIL import Image, ImageDraw, ImageFont
import pydenticon
import argparse
from io import BytesIO

parser = argparse.ArgumentParser(description="Generates cards from Bluesky profile data")
parser.add_argument("actor", help="The handle or DID of the user to generate a card for")

# Matches most emoji, flags, ZWJ sequences, and skin-tone modifiers.
# Characters that fall in these ranges get rendered with the emoji
# fallback font instead of InterVariable, which has no emoji glyphs.
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # regional indicator symbols (flags)
    "\U0001F300-\U0001FAFF"  # the bulk of emoji, incl. skin tone modifiers
    "\U00002600-\U000027BF"  # misc symbols & dingbats
    "\U00002B00-\U00002BFF"  # misc symbols and arrows (stars, etc.)
    "\U0001F000-\U0001F0FF"  # mahjong/dominoes/cards
    "\uFE0F"                  # variation selector-16 (forces emoji presentation)
    "\u200D"                  # zero-width joiner (combines emoji, e.g. family/profession emoji)
    "]+"
)


def format_bluesky_did(did):
  """
  Formats a Bluesky DID by shortening the middle part.

  Args:
    did: The full Bluesky DID string.

  Returns:
    The formatted Bluesky DID string.
  """

  if len(did) <= 15:
    return did  # No need to shorten if already short

  # Shorten the middle part
  shortened_middle = did[8:13] + "..." + did[-5:]
  formatted_did = f"did:plc:{shortened_middle}"
  return formatted_did


def get_dominant_color(image):
  """
  Finds the dominant color in a PIL Image object.

  Args:
    image: A PIL Image object.

  Returns:
    A tuple representing the RGB values of the dominant color.
  """
  try:
    img = image.convert('RGB')
    img = img.resize((1, 1), resample=0)  # Reduce to 1x1 pixel
    dominant_color = img.getpixel((0, 0))
    return dominant_color
  except Exception as e:
    print(f"Error processing image: {e}")
    return None


def split_emoji_runs(text):
  """
  Splits text into a list of (segment, is_emoji) tuples, preserving order.
  Consecutive characters of the same kind (emoji or non-emoji) are grouped
  into a single run so fonts don't get switched more often than necessary.

  Args:
    text: The string to split.

  Returns:
    A list of (segment, is_emoji) tuples covering the whole string in order.
  """
  runs = []
  last_end = 0
  for match in EMOJI_PATTERN.finditer(text):
    if match.start() > last_end:
      runs.append((text[last_end:match.start()], False))
    runs.append((text[match.start():match.end()], True))
    last_end = match.end()
  if last_end < len(text):
    runs.append((text[last_end:], False))
  return runs


def measure_mixed_width(text, text_font, emoji_font, measurer):
  """
  Measures the pixel width of a string that may contain emoji, using
  text_font for normal characters and emoji_font for emoji runs.

  Args:
    text: The string to measure.
    text_font: The ImageFont used for non-emoji characters.
    emoji_font: The ImageFont used for emoji characters.
    measurer: An ImageDraw instance used to call textlength.

  Returns:
    The total width in pixels.
  """
  total = 0
  for segment, is_emoji in split_emoji_runs(text):
    font = emoji_font if is_emoji else text_font
    total += measurer.textlength(segment, font=font)
  return total


def draw_mixed_text(draw, xy, text, text_font, emoji_font, fill=(255, 255, 255)):
  """
  Draws a string that may contain emoji, switching fonts per run and
  advancing the x position as it goes.

  Args:
    draw: The ImageDraw instance to draw with.
    xy: An (x, y) tuple for the starting position.
    text: The string to draw.
    text_font: The ImageFont used for non-emoji characters.
    emoji_font: The ImageFont used for emoji characters.
    fill: The fill color for the text.

  Returns:
    The x position immediately after the drawn text.
  """
  x, y = xy
  for segment, is_emoji in split_emoji_runs(text):
    font = emoji_font if is_emoji else text_font
    draw.text((x, y), segment, font=font, fill=fill)
    x += draw.textlength(segment, font=font)
  return x


def wrap_text(text, font, emoji_font, max_width):
  """
  Wraps text into a list of lines that each fit within max_width,
  measuring both normal and emoji characters. Respects existing
  newlines in the source text as hard line breaks.

  Args:
    text: The text to wrap.
    font: The ImageFont used to measure non-emoji characters.
    emoji_font: The ImageFont used to measure emoji characters.
    max_width: The maximum width in pixels a line may occupy.

  Returns:
    A list of wrapped lines.
  """
  if not text:
    return []

  measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))

  lines = []
  for paragraph in text.split("\n"):
    words = paragraph.split(" ")
    current_line = ""

    for word in words:
      candidate = word if not current_line else f"{current_line} {word}"
      if measure_mixed_width(candidate, font, emoji_font, measurer) <= max_width:
        current_line = candidate
      else:
        if current_line:
          lines.append(current_line)
        # Handle a single word longer than max_width by hard-breaking it
        if measure_mixed_width(word, font, emoji_font, measurer) > max_width:
          chunk = ""
          for char in word:
            if measure_mixed_width(chunk + char, font, emoji_font, measurer) <= max_width:
              chunk += char
            else:
              lines.append(chunk)
              chunk = char
          current_line = chunk
        else:
          current_line = word

    lines.append(current_line)

  return lines


def fit_text_to_width(text, font_path, variation, emoji_font_path, max_font_size, min_font_size, max_width):
  """
  Finds the largest font size (between min and max) at which a single
  line of text, possibly containing emoji, fits within max_width. If
  even min_font_size doesn't fit, truncates the text with an ellipsis
  until it does.

  Args:
    text: The text to fit on one line.
    font_path: Path to the regular text font file.
    variation: The named font variation to apply (e.g. "Bold").
    emoji_font_path: Path to the emoji fallback font file.
    max_font_size: The largest font size to try first.
    min_font_size: The smallest font size to fall back to.
    max_width: The maximum width in pixels the line may occupy.

  Returns:
    A tuple of (text_font, emoji_font, text) where text is the
    (possibly truncated) string to draw.
  """
  measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))

  # Step down font size until the full text fits, or we hit the minimum.
  # Text and emoji fonts are sized together so emoji scale with the text.
  for size in range(max_font_size, min_font_size - 1, -1):
    text_font = ImageFont.truetype(font_path, size=size)
    text_font.set_variation_by_name(variation)
    emoji_font = ImageFont.truetype(emoji_font_path, size=size)
    if measure_mixed_width(text, text_font, emoji_font, measurer) <= max_width:
      return text_font, emoji_font, text

  # Still doesn't fit at min size: truncate with an ellipsis
  text_font = ImageFont.truetype(font_path, size=min_font_size)
  text_font.set_variation_by_name(variation)
  emoji_font = ImageFont.truetype(emoji_font_path, size=min_font_size)

  ellipsis = "…"
  truncated = text
  while truncated and measure_mixed_width(truncated + ellipsis, text_font, emoji_font, measurer) > max_width:
    truncated = truncated[:-1]

  final_text = (truncated + ellipsis) if truncated != text else text
  return text_font, emoji_font, final_text


def main():
    args = parser.parse_args()

    global profile
    global avatar
    global main_image

    bsky_url = "https://public.api.bsky.app/xrpc/"

    print("Acquiring profile data")
    response = requests.get(bsky_url + f"app.bsky.actor.getProfile/?actor={args.actor}")
    profile = response.json()

    print(f"User: {profile['displayName']} / @{profile['handle']} / {format_bluesky_did(profile['did'])}")
    print("Starting card generation")

    # Grab avatar for generation of image
    # Size of avatar (scaled 1.778x from original 200x200)
    avatar_size = (356, 356)

    try:
        if "avatar" in profile:
            avatar_response = requests.get(profile["avatar"], stream=True)

            avatar = Image.open(avatar_response.raw)
            avatar = avatar.resize(avatar_size)
        else:
            print("No avatar could be found.")
            print("An identicon-style image will be used instead.")

            avatar = pydenticon.Generator.generate(width=avatar_size[1], height=avatar_size[1], data=profile["did"])
            avatar = Image.open(BytesIO(avatar))
    except Exception:
        print("Failed to acquire avatar.")
        print("An identicon-style image will be used instead.")

        avatar = pydenticon.Generator.generate(width=avatar_size[1], height=avatar_size[1], data=profile["did"])
        avatar = Image.open(BytesIO(avatar))

    dominant_color = get_dominant_color(avatar)

    # Layout constants
    canvas_width = 1280
    left_margin = 36
    text_x = 427
    right_margin = 36
    description_y = 231
    description_font_size = 25
    line_spacing = 1.35  # multiplier on font size, gives a bit of breathing room
    font_path = "./InterVariable.ttf"
    emoji_font_path = "./NotoEmoji-Regular.ttf"
    max_text_width = canvas_width - text_x - right_margin

    # Fit the display name to one line, shrinking from 71px down to 32px
    # before falling back to truncation with an ellipsis. Emoji in the
    # name are measured and drawn with the matching-size emoji font.
    display_name = profile.get("displayName") or profile["handle"]
    name_font, name_emoji_font, display_name = fit_text_to_width(
        text=display_name,
        font_path=font_path,
        variation="Bold",
        emoji_font_path=emoji_font_path,
        max_font_size=71,
        min_font_size=32,
        max_width=max_text_width,
    )

    # Handle font (emoji are not expected in handles, but the fallback
    # font is loaded anyway in case of edge cases like custom domains)
    handle_font = ImageFont.truetype(font_path, size=36)
    handle_font.set_variation_by_name("SemiBold")
    handle_emoji_font = ImageFont.truetype(emoji_font_path, size=36)

    # Stats fonts
    stats_font = ImageFont.truetype(font_path, size=21)
    stats_font.set_variation_by_name("Regular")
    stats_emoji_font = ImageFont.truetype(emoji_font_path, size=21)

    # Description font, loaded up front so we can measure with it
    desc_font = ImageFont.truetype(font_path, size=description_font_size)
    desc_font.set_variation_by_name("Regular")
    desc_emoji_font = ImageFont.truetype(emoji_font_path, size=description_font_size)

    description_text = profile.get("description") or ""
    description_lines = wrap_text(description_text, desc_font, desc_emoji_font, max_text_width)

    line_height = int(description_font_size * line_spacing)
    description_block_height = len(description_lines) * line_height

    # Minimum height needed to fit the avatar comfortably (original card proportions)
    min_height = avatar_size[1] + (left_margin * 2)  # 356 + 36 top + 36 bottom

    # Height needed to fit the avatar column PLUS the wrapped description
    description_bottom_margin = 36
    content_height = description_y + description_block_height + description_bottom_margin

    canvas_height = max(min_height, content_height)

    main_image = Image.new(mode="RGB", size=(canvas_width, canvas_height), color=tuple(int(c * 0.4) for c in dominant_color))

    # insert avatar (scaled 1.778x from original 20,20)
    main_image.paste(avatar, (left_margin, left_margin))

    # initialize imagedraw
    main_image_draw = ImageDraw.Draw(main_image)

    # display name, font size already fitted above, emoji-aware
    draw_mixed_text(
        main_image_draw,
        (text_x, 36),
        display_name,
        name_font,
        name_emoji_font,
    )

    # handle
    draw_mixed_text(
        main_image_draw,
        (text_x, 116),
        f"@{profile['handle']}",
        handle_font,
        handle_emoji_font,
    )

    # stats
    draw_mixed_text(
        main_image_draw,
        (text_x, 169),
        f"{profile['postsCount']} posts | {profile['followersCount']} followers | {profile['followsCount']} following",
        stats_font,
        stats_emoji_font,
    )

    # stats 2
    draw_mixed_text(
        main_image_draw,
        (text_x, 199),
        f"Created {profile['associated']['lists']} lists, {profile['associated']['feedgens']} feeds, {profile['associated']['starterPacks']} starter packs",
        stats_font,
        stats_emoji_font,
    )

    # description, pre-wrapped to fit the canvas width, drawn line by line
    # so each line can mix the text font and emoji font as needed
    for i, line in enumerate(description_lines):
        draw_mixed_text(
            main_image_draw,
            (text_x, description_y + i * line_height),
            line,
            desc_font,
            desc_emoji_font,
        )

    main_image.save(f"{profile['did']}.png")


main()