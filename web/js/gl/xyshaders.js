// GLSL ES 3.00 sources for XYRenderer (ARCHITECTURE.md 6.9, tiers T1/T2).
//
// XY_VS feeds the vendored density passes (vendor/gc/gl/shaders.js ACCUM_FS_CONT / ACCUM_FS_CAT
// -> COMPOSITE_FS -> bloom). It reads already-projected positions instead of uint16 dims and
// emits exactly the varyings those fragment shaders consume: v_w, v_c, v_has, v_sel, flat v_code.
//
// MARKER_VS / MARKER_FS draw tier T1: alpha-blended antialiased round sprites with
// premultiplied output. Pass 0 draws unselected rows (dimmed by u_unselDim while a selection
// is shown); pass 1 draws selected rows on top, slightly larger, with a selection-colour ring.
//
// Attribute locations (shared by both vertex shaders, so one VAO serves every program):
//   0 a_x   float                 projected x (minus the CPU origin)
//   1 a_y   float                 projected y
//   2 a_vis float 0..1            UNSIGNED_BYTE normalized (kernel.js vis bytes)
//   3 a_cats uvec4                category codes of slots 0..3
//   4 a_sel float                 UNSIGNED_BYTE, nonzero = selected
//   5 a_val float                 colour value; <= NO_VALUE means missing

export const NO_VALUE = -3.0e38;

const COMMON = `
layout(location = 0) in float a_x;
layout(location = 1) in float a_y;
layout(location = 2) in float a_vis;
layout(location = 3) in uvec4 a_cats;
layout(location = 4) in float a_sel;
layout(location = 5) in float a_val;

uniform vec4 u_cam;         // (cx, cy, 2*scale/width, 2*scale*aspect/height)
uniform float u_pointSize;  // device px
uniform uvec4 u_catMask;    // visible-code bitmask per category slot
uniform int u_catSlot;      // category slot used for categorical colour (-1 none)
uniform int u_colorMode;    // 0 none, 1 continuous, 2 categorical
uniform vec2 u_colRange;    // (lo, 1/(hi-lo)); hi < lo reverses

const float MISSING_VAL = -1.0e37;

float visibility() {
  float vis = a_vis;
  uvec4 bits = (u_catMask >> min(a_cats, uvec4(31u))) & uvec4(1u);
  if (bits.x == 0u || bits.y == 0u || bits.z == 0u || bits.w == 0u) vis = 0.0;
  if (isnan(a_x) || isnan(a_y) || isinf(a_x) || isinf(a_y)) vis = 0.0;
  return vis;
}

uint catCode() {
  uint code = u_catSlot == 0 ? a_cats.x : u_catSlot == 1 ? a_cats.y : u_catSlot == 2 ? a_cats.z
            : u_catSlot == 3 ? a_cats.w : 0u;
  return code > 7u ? 0u : code;
}

vec4 clipPos() {
  return vec4((vec2(a_x, a_y) - u_cam.xy) * u_cam.zw, 0.0, 1.0);
}
`;

export const XY_VS = `#version 300 es
precision highp float;
precision highp int;
${COMMON}
out float v_w;
out float v_c;
out float v_has;
out float v_sel;
flat out uint v_code;

void main() {
  float vis = visibility();
  bool has = u_colorMode == 1 && a_val > MISSING_VAL;
  v_c = has ? clamp((a_val - u_colRange.x) * u_colRange.y, 0.0, 1.0) : 0.0;
  v_has = has ? 1.0 : 0.0;
  v_sel = min(a_sel, 1.0);
  v_code = catCode();
  v_w = vis;
  if (vis < 0.01) {
    gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
    gl_PointSize = 1.0;
    return;
  }
  gl_Position = clipPos();
  gl_PointSize = u_pointSize;
}
`;

export const MARKER_VS = `#version 300 es
precision highp float;
precision highp int;
${COMMON}
uniform sampler2D u_lut;
uniform float u_lutV;
uniform vec3 u_cat[8];
uniform vec3 u_neutral;
uniform vec3 u_selColor;
uniform int u_pass;          // 0 unselected (or all), 1 selected on top
uniform float u_selShow;     // 1 when a non-empty selection is shown
uniform float u_unselDim;    // alpha factor of unselected rows while a selection is shown
uniform float u_alpha;       // marker opacity
uniform float u_ringPx;      // ring width (device px) of selected markers

out vec3 v_rgb;
out float v_a;
out float v_ring;            // ring width as a fraction of the sprite radius (0 = none)
out float v_aa;              // antialiasing width as a fraction of the radius

void main() {
  float vis = visibility();
  bool sel = a_sel > 0.5;
  bool show = u_selShow > 0.5;
  if (u_pass == 1 && !sel) vis = 0.0;
  if (u_pass == 0 && show && sel) vis = 0.0;
  if (vis < 0.01) {
    gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
    gl_PointSize = 1.0;
    v_rgb = vec3(0.0); v_a = 0.0; v_ring = 0.0; v_aa = 1.0;
    return;
  }
  vec3 col = u_neutral;
  if (u_colorMode == 1 && a_val > MISSING_VAL) {
    float t = clamp((a_val - u_colRange.x) * u_colRange.y, 0.0, 1.0);
    col = textureLod(u_lut, vec2((t * 255.0 + 0.5) / 256.0, u_lutV), 0.0).rgb;
  } else if (u_colorMode == 2) {
    col = u_cat[catCode()];
  }
  float a = u_alpha * vis;
  if (u_pass == 0 && show) a *= u_unselDim;
  float size = u_pointSize;
  v_ring = 0.0;
  if (u_pass == 1) {
    size = u_pointSize * 1.15 + 2.0 * u_ringPx;
    v_ring = clamp(2.0 * u_ringPx / size, 0.0, 0.6);
    a = max(a, 0.9 * vis);
  }
  v_rgb = col;
  v_a = a;
  v_aa = clamp(2.0 / max(size, 1.0), 0.02, 1.0);
  gl_Position = clipPos();
  gl_PointSize = size;
}
`;

export const MARKER_FS = `#version 300 es
precision highp float;
in vec3 v_rgb;
in float v_a;
in float v_ring;
in float v_aa;
uniform vec3 u_selColor;
out vec4 o;
void main() {
  vec2 p = gl_PointCoord * 2.0 - 1.0;
  float r = length(p);
  float cover = 1.0 - smoothstep(1.0 - v_aa, 1.0, r);
  if (cover <= 0.0) discard;
  vec3 rgb = v_rgb;
  if (v_ring > 0.0) {
    float k = smoothstep(1.0 - v_ring - v_aa, 1.0 - v_ring, r);
    rgb = mix(v_rgb, u_selColor, k);
  }
  float a = v_a * cover;
  o = vec4(rgb * a, a);
}
`;
