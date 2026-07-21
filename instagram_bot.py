async def download_instagram(update: Update, context: ContextTypes.DEFAULT_TYPE):

    url = update.message.text.strip()

    if "instagram.com" not in url:
        await update.message.reply_text(
            "لطفاً یک لینک معتبر اینستاگرام بفرست."
        )
        return


    status_msg = await update.message.reply_text(
        "در حال بررسی محتوا... ⏳"
    )


    files = []


    try:

        ydl_opts = {
            "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
            "format": "best",
            "quiet": True,
            "noplaylist": False,
        }


        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )


        entries = info.get("entries")


        # اگر پست چندتایی باشد
        if entries:

            for item in entries:

                filename = ydl.prepare_filename(item)

                if os.path.exists(filename):
                    files.append(filename)


        else:

            filename = ydl.prepare_filename(info)

            if os.path.exists(filename):
                files.append(filename)



        # ارسال فایل ها

        for file in files:

            with open(file, "rb") as f:

                if file.lower().endswith(
                    (".mp4", ".mov", ".webm")
                ):

                    await update.message.reply_video(
                        video=f
                    )

                else:

                    await update.message.reply_photo(
                        photo=f
                    )



        # اگر چیزی پیدا نشد، احتمالاً عکس مستقیم است

        if not files:

            with yt_dlp.YoutubeDL({
                "quiet": True,
                "skip_download": True
            }) as ydl:

                info = ydl.extract_info(
                    url,
                    download=False
                )


            thumbnail = info.get(
                "thumbnail"
            )


            if thumbnail:

                data = requests.get(
                    thumbnail
                ).content


                photo_path = (
                    f"{DOWNLOAD_DIR}/photo.jpg"
                )


                with open(
                    photo_path,
                    "wb"
                ) as f:

                    f.write(data)


                with open(
                    photo_path,
                    "rb"
                ) as f:

                    await update.message.reply_photo(
                        photo=f
                    )


                os.remove(
                    photo_path
                )


            else:

                raise Exception(
                    "محتوا پیدا نشد"
                )


        await status_msg.delete()



    except Exception as e:

        await status_msg.edit_text(
            f"❌ خطا در دانلود:\n{e}"
        )



    finally:

        for file in files:

            if os.path.exists(file):

                os.remove(file)
