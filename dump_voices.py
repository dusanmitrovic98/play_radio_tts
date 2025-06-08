import asyncio
import edge_tts
import json

async def main():
    voices = await edge_tts.list_voices()
    with open('all_voices.json', 'w', encoding='utf-8') as f:
        json.dump({v['ShortName']: v['ShortName'] for v in voices}, f, ensure_ascii=False, indent=2)
    print('done')

if __name__ == '__main__':
    asyncio.run(main())
