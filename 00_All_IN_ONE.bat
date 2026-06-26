@echo off
setlocal enabledelayedexpansion

:: ====================================================================================
:: FFmpeg Drag & Drop - Max Quality HDR Upscale & Advanced 5.1 Surround Processor
::
:: This script combines video upscaling, HDR conversion, and advanced audio upmixing
:: for the highest possible quality YouTube upload.
::
:: Instructions:
:: 1. Save this file as "Process_Video_Max_Quality.bat".
:: 2. Place "ffmpeg.exe" and "S-Log3_to_BT.2020_HDR.cube" in the same folder as this script.
:: 3. Drag and drop a single video file onto this .bat file icon.
::
:: The script will create a new file in the same directory with "_HDR_5.1_Max_Quality"
:: appended to the original filename.
:: ====================================================================================

REM --- SCRIPT CONFIGURATION ---
REM Set the path to your ffmpeg executable. If it's in the same folder, you can just leave "ffmpeg".
set "FFMPEG_EXE=ffmpeg"
REM Set the name of your LUT file. It must be in the same folder as this script.
set "LUT_FILE=SDR-to_HDR.cube"
REM Set the CRF (Constant Rate Factor). Lower is better quality. 18 is visually lossless for x265.
set "CRF_VALUE=18"


REM --- SCRIPT START ---
echo.
echo ========================================================
echo   Max Quality Advanced HDR 4K & 5.1 Surround Processor
echo ========================================================
echo.

REM --- VALIDATION CHECKS ---
REM Check if a file was dropped on the script
if "%~1"=="" (
    echo [ERROR] No input file detected.
    echo Please drag and drop a video file onto this script.
    echo.
    goto :end
)

REM Check if the required LUT file exists
if not exist "%LUT_FILE%" (
    echo [ERROR] LUT file not found!
    echo Please make sure "%LUT_FILE%" is in the same folder as this script.
    echo.
    goto :end
)


REM --- FILE SETUP ---
REM Get the full path of the dropped file
set "INPUT_FILE=%~1"
REM Create the output filename by adding a suffix before the extension
set "OUTPUT_FILE=%~dpn1_HDR_5.1_Max_Quality.mp4"

echo [INFO] Input File: "%INPUT_FILE%"
echo [INFO] Output File: "%OUTPUT_FILE%"
echo.
echo [INFO] Starting FFmpeg process... This will take a very long time.
echo --------------------------------------------------------


REM --- FFMPEG COMMAND ---
REM This command combines video and a complex audio filter graph.
REM -preset slow is now a separate option for better compression efficiency.
REM -crf %CRF_VALUE% is used instead of a fixed bitrate for higher quality.
REM Audio bitrate is increased to 640k for maximum quality AAC 5.1.

"%FFMPEG_EXE%" -i "%INPUT_FILE%" ^
    -vf "scale=3840:2160:flags=lanczos,lut3d=file='%LUT_FILE%'" ^
    -filter_complex "[0:a]dynaudnorm,extrastereo=m=1.2[wide];[wide]surround=5.1[s_mix];[s_mix]channelsplit=channel_layout=5.1[FL][FR][FC][LFE][BL][BR];[FC]dialoguenhance=original=0.1[FC_enhanced];[BL]volume=2.0,adelay=20[BL_d];[BR]volume=2.0,adelay=20[BR_d];[BL_d][BR_d]join=inputs=2[bs_stereo];[bs_stereo]stereowiden=delay=20:crossfeed=0.8:feedback=0.2[bs_wide];[bs_wide]channelsplit=channel_layout=stereo[BL_final][BR_final];[FL][FR][FC_enhanced][LFE][BL_final][BR_final]join=inputs=6:channel_layout=5.1:map=0.0-FL|1.0-FR|2.0-FC|3.0-LFE|4.0-BL|5.0-BR[final_audio]" ^
    -map 0:v ^
    -map "[final_audio]" ^
    -c:v libx265 ^
    -preset slow ^
    -crf %CRF_VALUE% ^
    -pix_fmt yuv420p10le ^
    -x265-params "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,500):max-cll=1000,400" ^
    -color_range tv ^
    -g 30 ^
    -movflags +faststart ^
    -c:a aac ^
    -b:a 640k ^
    -y "%OUTPUT_FILE%"


REM --- COMPLETION ---
echo --------------------------------------------------------
echo [SUCCESS] FFmpeg process finished!
echo Your new file is located at: "%OUTPUT_FILE%"
echo.

:end
echo Press any key to exit.
pause > nul
exit
