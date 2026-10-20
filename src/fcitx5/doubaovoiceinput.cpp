#include <cstdint>
#include <memory>
#include <string>
#include <utility>

#include <fcitx-config/configuration.h>
#include <fcitx-config/iniparser.h>
#include <fcitx-config/option.h>
#include <fcitx-utils/capabilityflags.h>
#include <fcitx-utils/dbus/objectvtable.h>
#include <fcitx-utils/i18n.h>
#include <fcitx-utils/key.h>
#include <fcitx-utils/keysym.h>
#include <fcitx-utils/log.h>
#include <fcitx-utils/trackableobject.h>
#include <dbus_public.h>
#include <fcitx/addonfactory.h>
#include <fcitx/addoninstance.h>
#include <fcitx/addonmanager.h>
#include <fcitx/event.h>
#include <fcitx/inputcontext.h>
#include <fcitx/inputpanel.h>
#include <fcitx/instance.h>
#include <fcitx/text.h>

FCITX_DEFINE_LOG_CATEGORY(doubaovoiceinput, "doubaovoiceinput");

namespace fcitx {

FCITX_CONFIGURATION(
    DoubaoVoiceInputConfig,
    KeyListOption triggerKey{
        this,
        "TriggerKey",
        _("Hold-to-talk trigger key"),
        {},
        KeyListConstrain(),
    };);

class DoubaoVoiceInput final : public AddonInstance,
                               public dbus::ObjectVTable<DoubaoVoiceInput> {
public:
    explicit DoubaoVoiceInput(Instance *instance) : instance_(instance) {
        reloadConfig();

        auto *dbusAddon = instance_->addonManager().addon("dbus", true);
        if (!dbusAddon) {
            FCITX_LOGC(doubaovoiceinput, Error) << "DBus addon is unavailable";
            return;
        }
        auto *bus = dbusAddon->call<IDBusModule::bus>();
        if (!bus) {
            FCITX_LOGC(doubaovoiceinput, Error) << "Cannot access session bus";
            return;
        }
        bus->addObjectVTable(
            "/doubao_voice_input",
            "org.fcitx.Fcitx.DoubaoVoiceInput1",
            *this);

        keyWatcher_ = instance_->watchEvent(
            EventType::InputContextKeyEvent,
            EventWatcherPhase::PreInputMethod,
            [this](Event &event) {
                handleKeyEvent(static_cast<KeyEvent &>(event));
            });
        focusWatcher_ = instance_->watchEvent(
            EventType::InputContextFocusOut,
            EventWatcherPhase::PreInputMethod,
            [this](Event &event) {
                auto *context =
                    static_cast<InputContextEvent &>(event).inputContext();
                if (context != target_.get()) {
                    return;
                }
                clearTargetPreedit();
                target_.unwatch();
                held_ = false;
                heldKeyCode_ = 0;
                cancelRequested();
            });
        destroyWatcher_ = instance_->watchEvent(
            EventType::InputContextDestroyed,
            EventWatcherPhase::PreInputMethod,
            [this](Event &event) {
                auto *context =
                    static_cast<InputContextEvent &>(event).inputContext();
                if (context != target_.get()) {
                    return;
                }
                target_.unwatch();
                held_ = false;
                heldKeyCode_ = 0;
                cancelRequested();
            });

        FCITX_LOGC(doubaovoiceinput, Info) << "Plugin loaded";
    }

    ~DoubaoVoiceInput() override {
        keyWatcher_.reset();
        focusWatcher_.reset();
        destroyWatcher_.reset();
    }

    void reloadConfig() override {
        readAsIni(config_, configFile());
        rebuildTriggerKeys();
    }

    const Configuration *getConfig() const override { return &config_; }

    void setConfig(const RawConfig &rawConfig) override {
        config_.load(rawConfig, true);
        rebuildTriggerKeys();
    }

    bool setTriggerKey(const std::string &keyString) {
        auto keys = Key::keyListFromString(keyString);
        eraseUnsupportedKeys(keys);
        if (keys.empty()) {
            FCITX_LOGC(doubaovoiceinput, Warn)
                << "Rejected trigger key: " << keyString;
            return false;
        }
        triggerKeys_ = std::move(keys);
        held_ = false;
        heldKeyCode_ = 0;
        FCITX_LOGC(doubaovoiceinput, Info)
            << "Trigger key set to "
            << Key::keyListToString(triggerKeys_);
        return true;
    }

    bool setPreedit(const std::string &text) {
        auto *context = usableTarget();
        if (!context) {
            target_.unwatch();
            return false;
        }
        updatePreedit(context, text);
        return true;
    }

    bool commitText(const std::string &text) {
        auto *context = usableTarget();
        if (!context) {
            target_.unwatch();
            return false;
        }
        clearPreedit(context);
        if (!text.empty()) {
            context->commitString(text);
        }
        target_.unwatch();
        held_ = false;
        heldKeyCode_ = 0;
        return true;
    }

    bool clearPreedit() {
        auto *context = target_.get();
        if (!context) {
            return false;
        }
        clearPreedit(context);
        if (!held_) {
            target_.unwatch();
        }
        return true;
    }

    FCITX_OBJECT_VTABLE_METHOD(setTriggerKey, "SetTriggerKey", "s", "b");
    FCITX_OBJECT_VTABLE_METHOD(setPreedit, "SetPreedit", "s", "b");
    FCITX_OBJECT_VTABLE_METHOD(commitText, "CommitText", "s", "b");
    FCITX_OBJECT_VTABLE_METHOD(clearPreedit, "ClearPreedit", "", "b");
    FCITX_OBJECT_VTABLE_SIGNAL(dictationKeyEvent, "DictationKeyEvent", "b");
    FCITX_OBJECT_VTABLE_SIGNAL(cancelRequested, "CancelRequested", "");
    FCITX_OBJECT_VTABLE_SIGNAL(sessionRejected, "SessionRejected", "s");

private:
    static constexpr const char *configFile() {
        return "conf/doubaovoiceinput.conf";
    }

    static bool isUsable(InputContext *context) {
        if (!context || !context->hasFocus()) {
            return false;
        }
        const auto capabilities = context->capabilityFlags();
        return !capabilities.testAny(CapabilityFlag::PasswordOrSensitive) &&
               !capabilities.test(CapabilityFlag::Disable);
    }

    void rebuildTriggerKeys() {
        triggerKeys_ = config_.triggerKey.value();
        eraseUnsupportedKeys(triggerKeys_);
        if (triggerKeys_.empty()) {
            triggerKeys_.push_back(Key("Super+space"));
        }
    }

    static void eraseUnsupportedKeys(KeyList &keys) {
        std::erase_if(keys, [](const Key &key) {
            return !key.isValid() || key.isModifier();
        });
    }

    bool isTriggerPress(const KeyEvent &event) const {
        if (event.rawKey().states().test(KeyState::Repeat) ||
            event.key().states().test(KeyState::Repeat)) {
            return false;
        }
        for (const auto &key : triggerKeys_) {
            if (event.key().check(key)) {
                return true;
            }
        }
        return false;
    }

    bool isHeldKeyRelease(const KeyEvent &event) const {
        if (heldKeyCode_ != 0) {
            return event.rawKey().code() == heldKeyCode_;
        }
        return event.key().check(triggerKeys_.front());
    }

    InputContext *usableTarget() const {
        auto *context = target_.get();
        return isUsable(context) ? context : nullptr;
    }

    static void updatePreedit(InputContext *context, const std::string &text) {
        Text preedit(text);
        preedit.setCursor(static_cast<int>(text.size()));
        auto &panel = context->inputPanel();
        panel.setClientPreedit(preedit);
        panel.setPreedit(preedit);
        context->updatePreedit(true);
        context->updateUserInterface(UserInterfaceComponent::InputPanel, true);
    }

    static void clearPreedit(InputContext *context) {
        auto &panel = context->inputPanel();
        panel.setClientPreedit(Text());
        panel.setPreedit(Text());
        context->updatePreedit(true);
        context->updateUserInterface(UserInterfaceComponent::InputPanel, true);
    }

    void clearTargetPreedit() {
        if (auto *context = target_.get()) {
            clearPreedit(context);
        }
    }

    void handleKeyEvent(KeyEvent &event) {
        const bool isRepeat =
            event.rawKey().states().test(KeyState::Repeat) ||
            event.key().states().test(KeyState::Repeat);
        FCITX_LOGC(doubaovoiceinput, Debug)
            << "key sym=" << std::hex << event.key().sym() << std::dec
            << " code=" << event.rawKey().code()
            << " rawStates=" << std::hex
            << event.rawKey().states().toInteger() << " states=" << std::hex
            << event.key().states().toInteger() << std::dec
            << " release=" << event.isRelease() << " repeat=" << isRepeat;
        if (held_ && isRepeat) {
            event.filterAndAccept();
            return;
        }

        if (event.isRelease()) {
            if (held_ && isHeldKeyRelease(event)) {
                held_ = false;
                heldKeyCode_ = 0;
                dictationKeyEvent(false);
                event.filterAndAccept();
            }
            return;
        }

        if (held_ &&
            event.key().sym() == FcitxKey_Escape) {
            held_ = false;
            heldKeyCode_ = 0;
            clearTargetPreedit();
            target_.unwatch();
            cancelRequested();
            event.filterAndAccept();
            return;
        }

        if (held_) {
            event.filterAndAccept();
            return;
        }

        if (!isTriggerPress(event)) {
            return;
        }

        event.filterAndAccept();
        auto *context = event.inputContext();
        if (!isUsable(context)) {
            sessionRejected("当前输入框不允许语音输入");
            return;
        }

        target_ = context->watch();
        held_ = true;
        heldKeyCode_ = event.rawKey().code();
        if (heldKeyCode_ == 0) {
            heldKeyCode_ = event.key().code();
        }
        dictationKeyEvent(true);
    }

    Instance *instance_;
    DoubaoVoiceInputConfig config_;
    KeyList triggerKeys_;
    TrackableObjectReference<InputContext> target_;
    bool held_ = false;
    int heldKeyCode_ = 0;
    std::unique_ptr<HandlerTableEntry<EventHandler>> keyWatcher_;
    std::unique_ptr<HandlerTableEntry<EventHandler>> focusWatcher_;
    std::unique_ptr<HandlerTableEntry<EventHandler>> destroyWatcher_;
};

class DoubaoVoiceInputFactory final : public AddonFactory {
public:
    AddonInstance *create(AddonManager *manager) override {
        return new DoubaoVoiceInput(manager->instance());
    }
};

} // namespace fcitx

FCITX_ADDON_FACTORY_V2(doubaovoiceinput, fcitx::DoubaoVoiceInputFactory)
