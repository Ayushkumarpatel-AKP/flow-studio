// One-click story starters shown in the Studio gallery.
export interface StoryTemplate {
  id: string;
  emoji: string;
  title: string;
  blurb: string;
  idea: string;
  style: string;
  tone: string;
  panels: number;
  color: string; // card accent
}

export const TEMPLATES: StoryTemplate[] = [
  { id: "zombie", emoji: "🧟", title: "Zombie Eclipse", blurb: "Survive the dark day",
    idea: "Two stickmen survive a zombie outbreak that starts during a solar eclipse and discover zombies freeze in sunlight",
    style: "stickman", tone: "funny", panels: 8, color: "#7ddc8a" },
  { id: "heist", emoji: "🍕", title: "Pizza Heist", blurb: "The tastiest crime",
    idea: "A clumsy stickman crew plans the perfect heist on a pizza shop's secret recipe, but everything goes hilariously wrong",
    style: "stickman", tone: "funny", panels: 6, color: "#ffb347" },
  { id: "space", emoji: "🚀", title: "Space Camping", blurb: "One small step... oops",
    idea: "Two astronauts go camping on the moon and everything that can go wrong does",
    style: "comic", tone: "funny", panels: 6, color: "#8ab4ff" },
  { id: "hero", emoji: "🦸", title: "Hero Origin", blurb: "A legend begins",
    idea: "An ordinary office worker gets accidental superpowers and must stop a villain before the Monday meeting",
    style: "comic", tone: "epic", panels: 8, color: "#ff7a8a" },
  { id: "haunted", emoji: "👻", title: "Haunted School", blurb: "Spooky but silly",
    idea: "A shy student discovers the school ghost is just lonely and wants a friend to help with homework",
    style: "anime", tone: "wholesome", panels: 6, color: "#c4a7ff" },
  { id: "robot", emoji: "🤖", title: "Robot Best Friend", blurb: "Too enthusiastic",
    idea: "A lonely stickman builds a robot friend that is far too enthusiastic about everything",
    style: "stickman", tone: "wholesome", panels: 6, color: "#6fe0e0" },
  { id: "dragon", emoji: "🐉", title: "Dragon Landlord", blurb: "Rent due in gold",
    idea: "A knight discovers his landlord is a dragon who wants the rent paid in gold, and the knight is broke",
    style: "watercolor", tone: "funny", panels: 6, color: "#ffd23f" },
  { id: "detective", emoji: "🕵️", title: "Detective Cat", blurb: "Who stole the tuna?",
    idea: "A grumpy detective cat investigates who stole the tuna from the fridge, and every suspect has an alibi",
    style: "comic", tone: "dark", panels: 8, color: "#ff9ecb" },
];

export const TONES = [
  { key: "funny", label: "😂 Funny" },
  { key: "epic", label: "⚡ Epic" },
  { key: "dark", label: "🌑 Dark" },
  { key: "wholesome", label: "💖 Wholesome" },
];

export const STYLE_META: Record<string, { label: string; emoji: string }> = {
  stickman: { label: "Stickman", emoji: "🕴️" },
  comic: { label: "Comic book", emoji: "💥" },
  anime: { label: "Anime", emoji: "🌸" },
  watercolor: { label: "Watercolor", emoji: "🎨" },
};
