import gc
import os
import copy
import numpy as np
from PIL import Image
import torch
import gradio as gr
from collections import OrderedDict
from scipy.ndimage import binary_dilation
from modules import scripts, shared
from modules.ui import gr_show
from modules.safe import unsafe_torch_load, load
from modules.processing import StableDiffusionProcessingImg2Img
from modules.devices import device, torch_gc, cpu
from segment_anything import SamPredictor, sam_model_registry
from modules.paths import models_path

model_cache = OrderedDict()

scripts_sam_model_dir = os.path.join(scripts.basedir(), "models/sam") 
sd_sam_model_dir = os.path.join(models_path, "sam")
sam_model_dir = sd_sam_model_dir if os.path.exists(sd_sam_model_dir) else scripts_sam_model_dir 

model_list = [f for f in os.listdir(sam_model_dir) if os.path.isfile(
    os.path.join(sam_model_dir, f)) and f.split('.')[-1] != 'txt']


refresh_symbol = '\U0001f504'       # 🔄


class ToolButton(gr.Button, gr.components.FormComponent):
    """Small button with single emoji as text, fits inside gradio forms"""

    def __init__(self, **kwargs):
        super().__init__(variant="tool", **kwargs)

    def get_block_name(self):
        return "button"


def show_mask(image_np, mask, random_color=False, alpha=0.5):
    image = copy.deepcopy(image_np)
    if random_color:
        color = np.concatenate([np.random.random(3), np.array([0.6])], axis=0)
    else:
        color = np.array([30/255, 144/255, 255/255, 0.6])
    image[mask] = image[mask] * (1 - alpha) + 255 * \
        color.reshape(1, 1, -1) * alpha
    return image.astype(np.uint8)


def load_sam_model(sam_checkpoint):
    model_type = '_'.join(sam_checkpoint.split('_')[1:-1])
    sam_checkpoint = os.path.join(sam_model_dir, sam_checkpoint)
    torch.load = unsafe_torch_load
    sam = sam_model_registry[model_type](checkpoint=sam_checkpoint)
    sam.to(device=device)
    torch.load = load
    return sam


def clear_sam_cache():
    model_cache.clear()
    gc.collect()
    torch_gc()

def update_mask(mask_gallery, chosen_mask, dilation_amt, input_image):
    print("Dilation Amount: ", dilation_amt)
    mask_image = Image.open(mask_gallery[chosen_mask + 3]['name'])
    binary_img = np.array(mask_image.convert('1'))
    if dilation_amt:
        # Convert the image to a binary numpy array
        mask_image, binary_img = dilate_mask(binary_img, dilation_amt)
    
    blended_image = Image.fromarray(show_mask(np.array(input_image), binary_img.astype(np.bool_)))
    return [blended_image, mask_image]

def refresh_sam_models(*inputs):
    global model_list
    model_list = [f for f in os.listdir(sam_model_dir) if os.path.isfile(
        os.path.join(sam_model_dir, f)) and f.split('.')[-1] != 'txt']
    dd = inputs[0]
    if dd in model_list:
        selected = dd
    elif len(model_list) > 0:
        selected = model_list[0]
    else:
        selected = None
    return gr.Dropdown.update(choices=model_list, value=selected)

def dilate_mask(mask, dilation_amt):
    # Create a dilation kernel
    x, y = np.meshgrid(np.arange(dilation_amt), np.arange(dilation_amt))
    center = dilation_amt // 2
    dilation_kernel = ((x - center)**2 + (y - center)**2 <= center**2).astype(np.uint8)

    # Dilate the image
    dilated_binary_img = binary_dilation(mask, dilation_kernel)

    # Convert the dilated binary numpy array back to a PIL image
    dilated_mask = Image.fromarray(dilated_binary_img.astype(np.uint8) * 255)

    return dilated_mask, dilated_binary_img


def sam_predict(model_name, input_image, positive_points, negative_points):
    print("Initializing SAM")
    image_np = np.array(input_image)
    image_np_rgb = image_np[..., :3]

    if model_name in model_cache:
        sam = model_cache[model_name]
        if shared.cmd_opts.lowvram:
            sam.to(device=device)
    elif model_name in model_list:
        clear_sam_cache()
        model_cache[model_name] = load_sam_model(model_name)
        sam = model_cache[model_name]
    else:
        Exception(
            f"{model_name} not found, please download model to models/sam.")

    predictor = SamPredictor(sam)
    print(f"Running SAM Inference {image_np_rgb.shape}")
    predictor.set_image(image_np_rgb)
    point_coords = np.array(positive_points + negative_points)
    point_labels = np.array(
        [1] * len(positive_points) + [0] * len(negative_points))
    masks, _, _ = predictor.predict(
        point_coords=point_coords,
        point_labels=point_labels,
        multimask_output=True,
    )
    if shared.cmd_opts.lowvram:
        sam.to(cpu)
    gc.collect()
    torch_gc()
    print("Creating output image")
    masks_gallery = []
    mask_images = []
    for mask in masks:
        blended_image = show_mask(image_np, mask)
        masks_gallery.append(Image.fromarray(mask))
        mask_images.append(Image.fromarray(blended_image))
    return mask_images + masks_gallery


class Script(scripts.Script):

    def title(self):
        return 'Segment Anything'

    def show(self, is_img2img):
        # TODO: Here I bypassed a bug inside module.img2img line 154, should be scripts_img2img instead.
        # return scripts.AlwaysVisible if is_img2img else False
        return scripts.AlwaysVisible

    def ui(self, is_img2img):
        # if is_img2img:
        with gr.Accordion('Segment Anything', open=False, elem_id=id('accordion'), visible=is_img2img):
            with gr.Column():
                gr.HTML(value="<p>Left click the image to add one positive point (black dot). Right click the image to add one negative point (red dot). Left click the point to remove it.</p>", label="Positive points")
                with gr.Row():
                    model_name = gr.Dropdown(label="Model", elem_id="sam_model", choices=model_list,
                                            value=model_list[0] if len(model_list) > 0 else None)
                    refresh_models = ToolButton(value=refresh_symbol)
                    refresh_models.click(refresh_sam_models, model_name, model_name)

                input_image = gr.Image(label="Image for Segment Anything", elem_id="sam_input_image",
                                    show_label=False, source="upload", type="pil", image_mode="RGBA")
                dummy_component = gr.Label(visible=False)
                mask_image = gr.Gallery(label='Segment Anything Output', show_label=False, elem_id='sam_gallery').style(grid=3)

                with gr.Row(elem_id="sam_generate_box", elem_classes="generate-box"):
                    gr.Button(value="You cannot preview segmentation because you have not added dot prompt.", elem_id="sam_no_button")
                    run_button = gr.Button(value="Preview Segmentation", elem_id="sam_run_button")
                
                gr.Checkbox(value=False, label="Preview automatically", elem_id="sam_realtime_preview_checkbox")

                with gr.Row():
                    enabled = gr.Checkbox(value=False, label="Copy to Inpaint Upload", elem_id="sam_impaint_checkbox")
                    chosen_mask = gr.Radio(label="Choose your favorite mask: ", value="0", choices=["0", "1", "2"], type="index")

                dilation_checkbox = gr.Checkbox(value=False, label="Expand Mask", elem_id="dilation_enable_checkbox")
                with gr.Column(visible=False) as dilation_column:
                    dilation_amt = gr.Slider(minimum=0, maximum=100, default=30, value=0, label="Specify the amount that you wish to expand the mask by (recommend 30)", elem_id="dilation_amt")
                    expanded_mask_image = gr.Gallery(label="Expanded Mask", elem_id="sam_expanded_mask").style(grid=2)
                    update_mask_button = gr.Button(value="Update Mask", elem_id="update_mask_button")

            run_button.click(
                fn=sam_predict,
                _js='submit_sam',
                inputs=[model_name, input_image,
                        dummy_component, dummy_component],
                outputs=[mask_image])
            
            dilation_checkbox.change(
                fn=gr_show,
                inputs=[dilation_checkbox],
                outputs=[dilation_column],
                show_progress=False)

            update_mask_button.click(
                fn=update_mask,
                inputs=[mask_image, chosen_mask, dilation_amt, input_image],
                outputs=[expanded_mask_image])
        
        return [enabled, input_image, mask_image, chosen_mask, dilation_checkbox, expanded_mask_image]

    def process(self, p: StableDiffusionProcessingImg2Img, enabled=False, input_image=None, mask=None, chosen_mask=0, dilation_enabled=False, expanded_mask=None):
        if not enabled or input_image is None or mask is None or not isinstance(p, StableDiffusionProcessingImg2Img):
            return
        p.init_images = [input_image]
        p.image_mask = Image.open(expanded_mask[1]['name'] if dilation_enabled and expanded_mask is not None else mask[chosen_mask + 3]['name'])
