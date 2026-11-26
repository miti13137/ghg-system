# ghg-system
企業溫室氣體盤查管理系統 ISO 14064-1

## Windows 開發預覽

適用已安裝 Python 3.10 或以上版本的 Windows 電腦。完整解壓縮後，執行 `Install-GHG.bat`，再從桌面開啟「GHG System V2」。也可以直接執行 `Start-GHG.bat` 試用。程式啟動後會開啟本機瀏覽器，使用期間請保持程式視窗開啟。

更新前請在「報表與備份」建立備份並關閉舊版程式。完整解壓新的版本後，再執行 `Install-GHG.bat`。安裝程式會在 `%LOCALAPPDATA%\GHGSystemApp` 建立新版程式目錄，更新桌面捷徑。盤查及佐證資料仍位於 `%LOCALAPPDATA%\GHGSystem`。舊程式資料夾可作比對，請保留資料目錄及備份。安裝與執行皆使用目前登入的 Windows 帳號。

本機備份還原、V1.0 移轉及目前測試範圍請見 [開發與操作說明](DEVELOPMENT_STATUS.md)。現階段請以模擬資料試用。桌面捷徑、Windows 更新與列印尚待實機驗證。
