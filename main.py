import os
import random
import sys
import pyautogui
import time

print(os.getcwd())

pyautogui.FAILSAFE = False


threshold = 0.9

def get_application_path():
    if getattr(sys, 'frozen', False):
        # Running in a frozen environment, use sys.executable to get the executable path
        application_path = os.path.dirname(sys.executable)
    else:
        # Running in a non-frozen environment, use __file__ to get the script path
        application_path = os.path.dirname(os.path.realpath(__file__))
    return application_path

def auto_clicker():

    current_dir = get_application_path()
    pictures_dir = os.path.join(current_dir, "pictures")
    items = os.listdir(pictures_dir)
    folders = [item for item in items if os.path.isdir(os.path.join(pictures_dir, item))]
    while True:
        for folder in folders:
            dir_path = os.path.join(pictures_dir, folder)
            entries = os.listdir(dir_path)
            images = [entry for entry in entries if os.path.isfile(os.path.join(dir_path, entry))]
            for image in images:
                image_path = os.path.join(dir_path, image)
                try: 
                    application_location = pyautogui.locateOnScreen(image_path, confidence=threshold)
                
                    if application_location is not None:
                        # Click on the launcher download button if found
                        pyautogui.click(application_location.left + 5, application_location.top + 5)
                        print(f"Image found.")
                        break

                except pyautogui.ImageNotFoundException:
                    # Handle the exception (e.g., print a message)
                    print(f"Image not found. Going to next image in folder: {folder}")

            print(f"Finished processing folder {folder}, moving to the next one.\n")
            time.sleep(random.uniform(2.0, 3.0))
            
def main():
    auto_clicker()

if __name__ == "__main__":
    main()