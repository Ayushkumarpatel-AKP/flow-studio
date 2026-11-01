// One-click story starters shown in the Studio gallery, grouped by category.
// Cover and style pictures are real pictures drawn with the same prompts Studio uses
// (see public/samples), so what you see on the card is what the style produces.
export interface StoryTemplate {
  id: string;
  category: Category;
  emoji: string;
  title: string;
  blurb: string;
  idea: string;
  style: string;
  tone: string;
  panels: number;
  color: string; // card accent
  cover: string; // sample picture
}

export const CATEGORIES = ["Comedy", "Action", "Sci-Fi & Fantasy", "Spooky & Mystery", "Wholesome"] as const;
export type Category = (typeof CATEGORIES)[number];

const cover = (id: string) => `/samples/thumbs/tpl-${id}.jpg`;

export const TEMPLATES: StoryTemplate[] = [
  // ── Comedy
  { id: "heist", category: "Comedy", emoji: "🍕", title: "Pizza Heist", blurb: "The tastiest crime",
    idea: "A clumsy stickman crew plans the perfect heist on a pizza shop's secret recipe, but everything goes hilariously wrong",
    style: "stickman", tone: "funny", panels: 6, color: "#ffb347", cover: cover("heist") },
  { id: "kitchen", category: "Comedy", emoji: "🍳", title: "Kitchen Chaos", blurb: "Chef vs. rogue robot",
    idea: "A proud stickman chef fights a malfunctioning cooking robot that keeps turning every dish into a disaster",
    style: "stickman", tone: "funny", panels: 6, color: "#ffd23f", cover: cover("kitchen") },
  // ── Action
  { id: "zombie", category: "Action", emoji: "🧟", title: "Zombie Eclipse", blurb: "Survive the dark day",
    idea: "Two stickmen survive a zombie outbreak that starts during a solar eclipse and discover zombies freeze in sunlight",
    style: "stickman", tone: "funny", panels: 8, color: "#7ddc8a", cover: cover("zombie") },
  { id: "hero", category: "Action", emoji: "🦸", title: "Hero Origin", blurb: "A legend begins",
    idea: "An ordinary office worker gets accidental superpowers and must stop a villain before the Monday meeting",
    style: "comic", tone: "epic", panels: 8, color: "#ff7a8a", cover: cover("hero") },
  // ── Sci-Fi & Fantasy
  { id: "space", category: "Sci-Fi & Fantasy", emoji: "🚀", title: "Space Camping", blurb: "One small step... oops",
    idea: "Two astronauts go camping on the moon and everything that can go wrong does",
    style: "comic", tone: "funny", panels: 6, color: "#8ab4ff", cover: cover("space") },
  { id: "dragon", category: "Sci-Fi & Fantasy", emoji: "🐉", title: "Dragon Landlord", blurb: "Rent due in gold",
    idea: "A knight discovers his landlord is a dragon who wants the rent paid in gold, and the knight is broke",
    style: "watercolor", tone: "funny", panels: 6, color: "#b5e48c", cover: cover("dragon") },
  // ── Spooky & Mystery
  { id: "haunted", category: "Spooky & Mystery", emoji: "👻", title: "Haunted School", blurb: "Spooky but silly",
    idea: "A shy student discovers the school ghost is just lonely and wants a friend to help with homework",
    style: "anime", tone: "wholesome", panels: 6, color: "#c4a7ff", cover: cover("haunted") },
  { id: "detective", category: "Spooky & Mystery", emoji: "🕵️", title: "Detective Cat", blurb: "Who stole the tuna?",
    idea: "A grumpy detective cat investigates who stole the tuna from the fridge, and every suspect has an alibi",
    style: "comic", tone: "dark", panels: 8, color: "#ff9ecb", cover: cover("detective") },
  // ── Wholesome
  { id: "robot", category: "Wholesome", emoji: "🤖", title: "Robot Best Friend", blurb: "Too enthusiastic",
    idea: "A lonely stickman builds a robot friend that is far too enthusiastic about everything",
    style: "stickman", tone: "wholesome", panels: 6, color: "#6fe0e0", cover: cover("robot") },
];

// Ready to ship, but their cover pictures could not be drawn yet (Google Flow's daily limit).
// Run the sample generator again after the limit resets, then move these into TEMPLATES.
export const PENDING_COVERS: StoryTemplate[] = [
  { id: "timecafe", category: "Comedy", emoji: "☕", title: "Time Traveler's Coffee", blurb: "Dinosaurs in the queue",
    idea: "A time traveler just wants a quiet coffee, but a dinosaur and a robot from the future are queuing right behind him",
    style: "stickman", tone: "funny", panels: 6, color: "#ffb3c1", cover: cover("timecafe") },
  { id: "ninja", category: "Action", emoji: "🥷", title: "Rooftop Chase", blurb: "Moonlit ninja pursuit",
    idea: "A ninja chases a thief across moonlit city rooftops to recover a glowing bag before sunrise",
    style: "comic", tone: "epic", panels: 8, color: "#9aa7ff", cover: cover("ninja") },
  { id: "puppy", category: "Wholesome", emoji: "🐶", title: "Lost Puppy", blurb: "A rainy-day rescue",
    idea: "A little girl and her big fluffy dog search the rainy city for her lost puppy and find it safe in a cozy bakery",
    style: "anime", tone: "wholesome", panels: 6, color: "#ffc6a8", cover: cover("puppy") },
];

export const TONES = [
  { key: "funny", label: "😂 Funny" },
  { key: "epic", label: "⚡ Epic" },
  { key: "dark", label: "🌑 Dark" },
  { key: "wholesome", label: "💖 Wholesome" },
];

// `sample` is a real picture drawn in that style, so a beginner can tell the styles apart at a glance.
export const STYLE_META: Record<string, { label: string; emoji: string; sample: string; hint: string }> = {
  stickman: { label: "Stickman", emoji: "🕴️", sample: "/samples/thumbs/style-stickman.jpg", hint: "Simple round-headed characters in bold ink" },
  comic: { label: "Comic book", emoji: "💥", sample: "/samples/thumbs/style-comic.jpg", hint: "Inked superhero-comic look" },
  anime: { label: "Anime", emoji: "🌸", sample: "/samples/thumbs/style-anime.jpg", hint: "Clean cel shading and big expressions" },
  watercolor: { label: "Watercolor", emoji: "🎨", sample: "/samples/thumbs/style-watercolor.jpg", hint: "Soft painted storybook look" },
};
