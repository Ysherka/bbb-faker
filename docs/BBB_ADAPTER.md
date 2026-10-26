# Статус браузерного адаптера

В диагностике первой проверки обнаружена старая разметка чата BBB 2 и Greenlight 2.14.8.1. Исправления версии 0.1.1 требуют повторной проверки на конкретном сервере.

Исходные селекторы сверены с официальным репозиторием BigBlueButton, ветка develop:

- https://github.com/bigbluebutton/bigbluebutton/blob/develop/bigbluebutton-tests/playwright/core/elements.js
- https://github.com/bigbluebutton/bigbluebutton/blob/develop/bigbluebutton-html5/imports/ui/components/chat/chat-graphql/chat-message-list/page/chat-message/component.tsx
- https://github.com/bigbluebutton/bigbluebutton/blob/develop/bigbluebutton-html5/imports/ui/components/chat/chat-graphql/chat-message-list/page/chat-message/message-header/component.tsx
- https://github.com/bigbluebutton/bigbluebutton/blob/develop/bigbluebutton-html5/imports/ui/components/user-list/component.tsx

Поддержка этих элементов подтверждается локальным DOM-стендом, а не реальным развёртыванием университета. Разметка гостевой формы, локализация названий, счётчик, отображение авторов, сигнал завершения и длительность подключения аудио могут потребовать адаптации.

Дополнительно сверены официальные исходники BBB 2.6:

- https://github.com/bigbluebutton/bigbluebutton/blob/v2.6.0/bigbluebutton-html5/imports/ui/components/chat/time-window-list/time-window-chat-item/component.jsx
- https://github.com/bigbluebutton/bigbluebutton/blob/v2.6.0/bigbluebutton-html5/imports/ui/components/chat/time-window-list/time-window-chat-item/message-chat-item/component.jsx
- https://github.com/bigbluebutton/bigbluebutton/blob/v2.6.0/bigbluebutton-html5/imports/ui/components/chat/message-form/component.jsx

Сохранённая диагностика подтверждает наличие `p[data-test=chatUserMessageText]`, `div[data-test=chatButton][role=button]`, `button[data-test=sendMessageButton]` и `h1[data-test=meetingEndedModalTitle]`. Полной DOM-записи чата в старой диагностике нет, поэтому расположение общего блока автора/времени воспроизведено по официальному компоненту и проверено локально. В новой диагностике сохраняются выборка DOM чата и результат распознавания.

Наблюдение не использует серверный секрет, не обращается к административному API и не извлекает учётные данные. Диагностика сохраняет снимок экрана, ограниченный перечень атрибутов DOM, результаты распознавания и несколько фрагментов чата. Полный HTML страницы, cookies и session tokens не экспортируются.

При несовпадении разметки программа показывает ошибку/предупреждение. Неподтверждённая отправка не повторяется автоматически; отсутствие счётчика отключает выводы об уходе участников. В режиме наблюдения сообщения не отправляются, остальные действия (вход, аудио, выход) выполняются.
